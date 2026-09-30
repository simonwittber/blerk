from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
from dataclasses import dataclass, replace
from typing import NamedTuple

import httpx

from blerk import config, db, embedding
from blerk_cmd.util import Scope, index_root, scope_clause, scope_readings, to_relative


@dataclass
class QueryOptions:
    n: int = 10
    exts: list[str] | None = None
    min_score: float = 0.0
    reranker: config.Reranker | None = None
    directory: str = ""
    tags: dict[str, str] | None = None
    refs: bool = False
    verbose: bool = False
    embed_model: str = ""
    root: str = ""
    loose: bool = False

    def scope(self) -> Scope:
        return Scope(directory=self.directory, exts=self.exts or [], root=self.root, loose=self.loose)

# RRF smoothing constant: 60 is the standard value that prevents top ranks from dominating.
_RRF_K = 60
_OVERFETCH = 20


def to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n - 3] + "..."


def _no_headings_sql(exts: list[str]) -> str:
    return "" if ".md" in exts else "AND s.kind != 'heading'"


def _tag_clause(tags: dict[str, str]) -> tuple[str, list[str]]:
    if not tags:
        return "", []
    joins = []
    params: list[str] = []
    for i, (k, v) in enumerate(tags.items()):
        joins.append(f"JOIN symbol_tags t{i} ON t{i}.symbol_id = s.id AND t{i}.key=? AND t{i}.value=?")
        params.extend([k, v])
    return " ".join(joins), params


def print_refs(conn, symbol_id: int) -> None:
    callees = conn.execute(
        """
        SELECT s.name, f.path FROM symbol_refs r
        JOIN symbols s ON s.id = r.callee_id
        JOIN file_paths f ON f.file_id = s.file_id
        WHERE r.caller_id = ? LIMIT 10
        """,
        (symbol_id,),
    ).fetchall()
    for name, path in callees:
        print(f"calls: {name} ({path})")

    callers = conn.execute(
        """
        SELECT s.name, f.path FROM symbol_refs r
        JOIN symbols s ON s.id = r.caller_id
        JOIN file_paths f ON f.file_id = s.file_id
        WHERE r.callee_id = ? LIMIT 10
        """,
        (symbol_id,),
    ).fetchall()
    for name, path in callers:
        print(f"calledby: {name} ({path})")


def _vector_positions(conn, blob: bytes, k: int, opts: QueryOptions) -> dict[int, int]:
    exts = opts.exts or []
    scope_sql, scope_params = scope_clause(opts.scope())
    heading_sql = _no_headings_sql(exts)
    tag_sql, tag_params = _tag_clause(opts.tags or {})
    model_sql = "AND e.model = ?" if opts.embed_model else ""
    model_params = [opts.embed_model] if opts.embed_model else []
    rows = conn.execute(
        f"""
        SELECT s.id
        FROM embeddings e
        JOIN code_blocks cb ON cb.content_hash = e.content_hash {model_sql}
        JOIN symbols s ON s.id = cb.symbol_id
        JOIN file_paths f ON f.file_id = s.file_id
        {tag_sql}
        WHERE 1=1 {heading_sql} {scope_sql}
        ORDER BY vec_distance_cosine(e.vector, ?) ASC
        LIMIT ?
        """,
        (*model_params, *tag_params, *scope_params, blob, k),
    ).fetchall()
    return {row[0]: rank for rank, row in enumerate(rows)}


def _bm25_symbol_positions(conn, query_text: str, k: int, opts: QueryOptions) -> dict[int, int]:
    """BM25 over symbol names and descriptions."""
    if not query_text.strip():
        return {}
    exts = opts.exts or []
    scope_sql, scope_params = scope_clause(opts.scope())
    heading_sql = _no_headings_sql(exts)
    tag_sql, tag_params = _tag_clause(opts.tags or {})
    try:
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.rowid
            JOIN file_paths f ON f.file_id = s.file_id
            {tag_sql}
            WHERE symbols_fts MATCH ?
              {heading_sql} {scope_sql}
            ORDER BY symbols_fts.rank
            LIMIT ?
            """,
            (*tag_params, query_text, *scope_params, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: rank for rank, row in enumerate(rows)}


def _bm25_content_positions(conn, query_text: str, k: int, opts: QueryOptions) -> dict[int, int]:
    """BM25 over code block content."""
    if not query_text.strip():
        return {}
    exts = opts.exts or []
    scope_sql, scope_params = scope_clause(opts.scope())
    heading_sql = _no_headings_sql(exts)
    tag_sql, tag_params = _tag_clause(opts.tags or {})
    try:
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM code_blocks_fts
            JOIN code_blocks cb ON cb.id = code_blocks_fts.rowid
            JOIN symbols s ON s.id = cb.symbol_id
            JOIN file_paths f ON f.file_id = s.file_id
            {tag_sql}
            WHERE code_blocks_fts MATCH ?
              {heading_sql} {scope_sql}
            ORDER BY code_blocks_fts.rank
            LIMIT ?
            """,
            (*tag_params, query_text, *scope_params, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: rank for rank, row in enumerate(rows)}


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def _rerank(reranker: config.Reranker, query_text: str, rows: list) -> list:
    from blerk.config import _DEFAULT_RERANKER_PROMPT
    numbered = "\n".join(
        f"{i+1}. {kind} {name}({params}) in {path}" + (f"\n   {desc}" if desc else "")
        for i, (_, name, kind, path, _, _, desc, params) in enumerate(rows)
    )
    prompt = (reranker.prompt or _DEFAULT_RERANKER_PROMPT).replace("{query_text}", query_text).replace("{numbered}", numbered)
    headers: dict[str, str] = {}
    if reranker.api_key:
        headers["Authorization"] = f"Bearer {reranker.api_key}"
    payload: dict = {
        "model": reranker.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": reranker.max_tokens,
        "temperature": 0,
    }
    if not reranker.think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        r = httpx.post(
            reranker.endpoint.rstrip("/") + "/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=60.0,
        )
        if r.status_code != 200:
            return _rerank_failed(reranker, f"HTTP {r.status_code}: {r.text.strip()[:200]}", rows)
        text = r.json()["choices"][0]["message"]["content"].strip()
        indices = [int(t.strip()) - 1 for t in text.split(",") if t.strip().isdigit()]
        if not indices:
            return _rerank_failed(reranker, f"no ranking in reply: {text[:200]!r}", rows)
        seen: set[int] = set()
        reordered = []
        for i in indices:
            if 0 <= i < len(rows) and i not in seen:
                reordered.append(rows[i])
                seen.add(i)
        for i, row in enumerate(rows):
            if i not in seen:
                reordered.append(row)
        return reordered
    except Exception as e:
        return _rerank_failed(reranker, str(e), rows)


def _rerank_failed(reranker: config.Reranker, detail: str, rows: list) -> list:
    """Report a reranker that is switched on but not working, then fall back to the fused order.

    Falling back silently is what made an enabled reranker look like it was running for months while every call failed.
    """
    print(
        f"blerk: reranker ({reranker.model} at {reranker.endpoint}) failed, using unranked results. {detail}",
        file=sys.stderr,
    )
    return rows


class QueryResult(NamedTuple):
    id: int
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    description: str
    content: str
    params: str
    score: float


def query_symbols(
    conn,
    blob: bytes,
    query_text: str,
    opts: QueryOptions,
) -> list[QueryResult]:
    k = opts.n * _OVERFETCH

    vector = _vector_positions(conn, blob, k, opts)
    bm25_sym = _bm25_symbol_positions(conn, query_text, k, opts)
    bm25_content = _bm25_content_positions(conn, query_text, k, opts)

    all_ids = set(vector) | set(bm25_sym) | set(bm25_content)
    scores: dict[int, float] = {}
    for id_ in all_ids:
        score = 0.0
        if id_ in vector:
            score += _rrf_score(vector[id_])
        if id_ in bm25_sym:
            score += _rrf_score(bm25_sym[id_])
        if id_ in bm25_content:
            score += _rrf_score(bm25_content[id_])
        scores[id_] = score

    if opts.min_score > 0.0:
        scores = {id_: s for id_, s in scores.items() if s >= opts.min_score}

    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:opts.n]
    if not top_ids:
        return []

    placeholders = ",".join("?" * len(top_ids))
    rows = conn.execute(
        f"""
        SELECT
            s.id,
            s.name,
            s.kind,
            f.path,
            s.line,
            COALESCE(s.end_line, s.line),
            COALESCE(s.description, ''),
            COALESCE(s.params, '')
        FROM symbols s
        JOIN file_paths f ON f.file_id = s.file_id
        WHERE s.id IN ({placeholders})
        """,
        top_ids,
    ).fetchall()

    # Fetch block 0 content for each result.
    block_content: dict[int, str] = {}
    if rows:
        id_ph = ",".join("?" * len(top_ids))
        for bid, content in conn.execute(
            f"SELECT symbol_id, content FROM code_blocks"
            f" WHERE symbol_id IN ({id_ph}) AND block_index=0",
            top_ids,
        ).fetchall():
            block_content[bid] = content

    # Ranking adjustments applied after row fetch.
    # Fields and variables are leaf data; downweight so semantic types surface first.
    # Test files are rarely the answer to a concept search.
    # Having a description is deliberately not a signal here.
    # The describer runs on every function and method over min_describe_lines, so the flag means "described yet", not "meaningful".
    # Boosting on it ranked whatever the describer had reached above whatever it had not.
    # Descriptions still earn their place by feeding the FTS index and the embedding text.
    _TEST_MARKERS = ("/tests/", "/editmode/", "/playmode/", "/test/", "tests.cs", "test.cs")
    for id_, name, kind, path, line, end_line, desc, params in rows:
        mult = 1.0
        if kind in ("field", "variable"):
            mult *= 0.5
        p = path.lower()
        if any(m in p for m in _TEST_MARKERS):
            mult *= 0.5
        scores[id_] *= mult

    rows.sort(key=lambda r: scores[r[0]], reverse=True)

    r = opts.reranker
    if r and r.endpoint and r.model:
        rows = _rerank(r, query_text, rows)

    return [
        QueryResult(id_, name, kind, path, line, end_line, desc,
                    block_content.get(id_, ""), params, scores[id_])
        for id_, name, kind, path, line, end_line, desc, params in rows
    ]


def format_verbose(conn, results: list[QueryResult], refs: bool = False, root: str = "") -> str:
    lines: list[str] = []
    for i, r in enumerate(results):
        sig = f"({r.params})" if r.params else ""
        path = to_relative(r.path, root)
        header = f"[{i + 1}] {r.kind} {r.name}{sig}  {path}:{r.line}-{r.end_line}  score:{r.score:.3f}"
        if r.description:
            header += f"  {r.description}"
        lines.append(header)
        if r.content:
            for l in r.content.splitlines():
                lines.append("  " + l)
        if refs:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_refs(conn, r.id)
            lines.append(buf.getvalue().rstrip())
    return "\n".join(lines)


def format_compact(results: list[QueryResult], root: str = "") -> str:
    lines: list[str] = []
    for r in results:
        sig = f"({r.params})" if r.params else ""
        lines.append(f"{r.kind} {r.name}{sig}  {to_relative(r.path, root)}:{r.line}-{r.end_line}")
    return "\n".join(lines)


def query_scoped(conn, blob: bytes, query_text: str, opts: QueryOptions) -> tuple[list[QueryResult], bool]:
    """Run the query against each reading of the directory argument until one matches.

    The second element is True when a widened reading is what produced the results.
    """
    for i, scope in enumerate(scope_readings(opts.scope())):
        attempt = replace(opts, root=scope.root, loose=scope.loose)
        results = query_symbols(conn, blob, query_text, attempt)
        if results:
            return results, i > 0
    return [], False


def run_query(conn, blob: bytes, query_text: str, opts: QueryOptions) -> None:
    results, loose = query_scoped(conn, blob, query_text, opts)
    if not results:
        print(no_results(opts))
        return
    if loose:
        print(f"Nothing sits directly under '{opts.directory}', so this matches anywhere in the path.")
    if opts.verbose or opts.refs:
        print(format_verbose(conn, results, opts.refs, opts.root))
    else:
        print(format_compact(results, opts.root))


def no_results(opts: QueryOptions) -> str:
    """Describe the scope that was searched, so a caller can tell a bad path from an empty one."""
    where = opts.directory or (opts.root or "the whole index")
    line = f"No results in {where}."
    if opts.root:
        line += f" Index root: {opts.root}."
    if opts.exts:
        line += f" Extensions: {', '.join(opts.exts)}."
    return line


def snippet_search(conn, cfg: "config.Config", query_text: str, directory: str, n: int = 10) -> str:
    vec = embedding.embed(
        cfg.embedder.endpoint, cfg.embedder.model, query_text, cfg.embedder.api_key,
    )
    blob = to_blob(vec)
    root = index_root(cfg)
    opts = QueryOptions(n=n, directory=directory, verbose=True, embed_model=cfg.embedder.model, root=root)
    results, _ = query_scoped(conn, blob, query_text, opts)
    if not results:
        return ""
    return format_verbose(conn, results, root=root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("-n", type=int, default=10, help="number of results")
    parser.add_argument("--verbose", action="store_true", help="show full output with snippets and scores")
    parser.add_argument("--refs", action="store_true", help="show callers and callees for each result (implies --verbose)")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        metavar="KEY=VALUE", help="filter by symbol tag, e.g. visibility=public (repeatable)")
    parser.add_argument("query", help="query text")
    parser.add_argument("directory", nargs="?", default="",
                        help="restrict to this directory, relative to the index root or absolute (default: the whole root)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    vec = embedding.embed(cfg.embedder.endpoint, cfg.embedder.model, args.query,
                          cfg.embedder.api_key)
    blob = to_blob(vec)

    tag_filter: dict[str, str] = {}
    for t in args.tags:
        if "=" in t:
            k, v = t.split("=", 1)
            tag_filter[k.strip()] = v.strip()

    opts = QueryOptions(
        n=args.n,
        exts=args.exts or None,
        reranker=cfg.reranker if cfg.reranker.enabled else None,
        directory=args.directory,
        verbose=args.verbose or args.refs,
        refs=args.refs,
        tags=tag_filter or None,
        embed_model=cfg.embedder.model,
        root=index_root(cfg),
    )
    run_query(conn, blob, args.query, opts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
