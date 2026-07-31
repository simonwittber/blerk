from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
from typing import NamedTuple

import httpx

from blerk import config, db

_RRF_K = 60
_OVERFETCH = 20


def embed(endpoint: str, model: str, text: str) -> list[float]:
    r = httpx.post(
        endpoint + "/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=30.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text}")
    return r.json()["embedding"]


def to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n - 3] + "..."


def _ext_clause(exts: list[str]) -> tuple[str, list[str]]:
    if not exts:
        return "", []
    parts = " OR ".join("f.path LIKE ?" for _ in exts)
    return f"AND ({parts})", [f"%{e}" for e in exts]


def _dir_clause(directory: str) -> tuple[str, list[str]]:
    if not directory:
        return "", []
    norm = directory.replace("\\", "/")
    return "AND f.path LIKE ?", [f"%{norm}%"]


def _heading_clause(exts: list[str]) -> str:
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
        JOIN files   f ON f.id = s.file_id
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
        JOIN files   f ON f.id = s.file_id
        WHERE r.callee_id = ? LIMIT 10
        """,
        (symbol_id,),
    ).fetchall()
    for name, path in callers:
        print(f"calledby: {name} ({path})")


def _vector_ranks(conn, blob: bytes, k: int, exts: list[str], directory: str = "", tags: dict[str, str] | None = None) -> dict[int, int]:
    ext_sql, ext_params = _ext_clause(exts)
    dir_sql, dir_params = _dir_clause(directory)
    heading_sql = _heading_clause(exts)
    tag_sql, tag_params = _tag_clause(tags or {})
    rows = conn.execute(
        f"""
        SELECT s.id
        FROM embeddings e
        JOIN symbols s ON s.id = e.symbol_id
        JOIN files f ON f.id = s.file_id
        {tag_sql}
        WHERE 1=1 {heading_sql} {ext_sql} {dir_sql}
        ORDER BY vec_distance_cosine(e.vector, ?) ASC
        LIMIT ?
        """,
        (*tag_params, *ext_params, *dir_params, blob, k),
    ).fetchall()
    return {row[0]: rank for rank, row in enumerate(rows)}


def _bm25_ranks(conn, query_text: str, k: int, exts: list[str], directory: str = "", tags: dict[str, str] | None = None) -> dict[int, int]:
    if not query_text.strip():
        return {}
    ext_sql, ext_params = _ext_clause(exts)
    dir_sql, dir_params = _dir_clause(directory)
    heading_sql = _heading_clause(exts)
    tag_sql, tag_params = _tag_clause(tags or {})
    try:
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.rowid
            JOIN files f ON f.id = s.file_id
            {tag_sql}
            WHERE symbols_fts MATCH ?
              {heading_sql} {ext_sql} {dir_sql}
            ORDER BY symbols_fts.rank
            LIMIT ?
            """,
            (*tag_params, query_text, *ext_params, *dir_params, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: rank for rank, row in enumerate(rows)}


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def _rerank(
    endpoint: str,
    model: str,
    api_key: str,
    query_text: str,
    rows: list,
) -> list:
    numbered = "\n".join(
        f"{i+1}. {kind} {name}({params}) in {path}" + (f"\n   {desc}" if desc else "")
        for i, (_, name, kind, path, _, _, desc, snippet, params) in enumerate(rows)
    )
    prompt = (
        f'Rank these code symbols by relevance to: "{query_text}"\n'
        f"Reply with only comma-separated indices, most relevant first.\n\n"
        f"{numbered}"
    )
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = httpx.post(
            endpoint + "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 64,
                "temperature": 0,
            },
            headers=headers,
            timeout=30.0,
        )
        if r.status_code != 200:
            return rows
        text = r.json()["choices"][0]["message"]["content"].strip()
        indices = [int(t.strip()) - 1 for t in text.split(",") if t.strip().isdigit()]
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
    except Exception:
        return rows


class QueryResult(NamedTuple):
    id: int
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    description: str
    snippet: str
    params: str
    score: float


def query_symbols(
    conn,
    blob: bytes,
    query_text: str,
    n: int,
    exts: list[str] | None = None,
    min_score: float = 0.0,
    reranker_endpoint: str = "",
    reranker_model: str = "",
    reranker_api_key: str = "",
    directory: str = "",
    tags: dict[str, str] | None = None,
) -> list[QueryResult]:
    k = n * _OVERFETCH
    exts = exts or []

    vector = _vector_ranks(conn, blob, k, exts, directory, tags)
    bm25 = _bm25_ranks(conn, query_text, k, exts, directory, tags)

    all_ids = set(vector) | set(bm25)
    scores: dict[int, float] = {}
    for id_ in all_ids:
        score = 0.0
        if id_ in vector:
            score += _rrf_score(vector[id_])
        if id_ in bm25:
            score += _rrf_score(bm25[id_])
        scores[id_] = score

    if min_score > 0.0:
        scores = {id_: s for id_, s in scores.items() if s >= min_score}

    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:n]
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
            COALESCE(s.snippet, ''),
            COALESCE(s.params, '')
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.id IN ({placeholders})
        """,
        top_ids,
    ).fetchall()

    # Ranking adjustments applied after row fetch.
    # Fields and variables are leaf data; downweight so semantic types surface first.
    # Test files are rarely the answer to a concept search.
    # AI-described symbols have already been judged meaningful; give them a boost.
    _TEST_MARKERS = ("/tests/", "/editmode/", "/playmode/", "/test/", "tests.cs", "test.cs")
    for id_, name, kind, path, line, end_line, desc, snippet, params in rows:
        mult = 1.0
        if kind in ("field", "variable"):
            mult *= 0.5
        p = path.lower()
        if any(m in p for m in _TEST_MARKERS):
            mult *= 0.5
        if desc:
            mult *= 2.5
        scores[id_] *= mult

    rows.sort(key=lambda r: scores[r[0]], reverse=True)

    if reranker_endpoint and reranker_model:
        rows = _rerank(reranker_endpoint, reranker_model, reranker_api_key, query_text, rows)

    return [
        QueryResult(id_, name, kind, path, line, end_line, desc, snippet, params, scores[id_])
        for id_, name, kind, path, line, end_line, desc, snippet, params in rows
    ]


def format_verbose(conn, results: list[QueryResult], refs: bool = False) -> str:
    lines: list[str] = []
    for i, r in enumerate(results):
        sig = f"({r.params})" if r.params else ""
        lines.append(f"[{i + 1}] {r.kind} {r.name}{sig}")
        lines.append(f"path: {r.path}")
        lines.append(f"lines: {r.line}-{r.end_line}")
        lines.append(f"score: {r.score:.3f}")
        if r.description:
            lines.append(f"desc: {r.description}")
        if r.snippet:
            indented = "\n".join("  " + l for l in r.snippet.splitlines())
            lines.append(f"snippet:\n{indented}")
        if refs:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_refs(conn, r.id)
            lines.append(buf.getvalue().rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


def format_compact(results: list[QueryResult]) -> str:
    lines: list[str] = []
    for r in results:
        sig = f"({r.params})" if r.params else ""
        lines.append(f"{r.kind} {r.name}{sig}  {r.path}:{r.line}-{r.end_line}")
        lines.append("")
    return "\n".join(lines).rstrip()


def run_query(
    conn,
    blob: bytes,
    query_text: str,
    n: int,
    refs: bool,
    exts: list[str] | None = None,
    min_score: float = 0.0,
    reranker_endpoint: str = "",
    reranker_model: str = "",
    reranker_api_key: str = "",
    directory: str = "",
    verbose: bool = False,
    tags: dict[str, str] | None = None,
) -> None:
    results = query_symbols(conn, blob, query_text, n, exts, min_score,
                            reranker_endpoint, reranker_model, reranker_api_key, directory, tags)
    if results:
        if verbose or refs:
            print(format_verbose(conn, results, refs))
        else:
            print(format_compact(results))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("-n", type=int, default=10, help="number of results")
    parser.add_argument("--verbose", action="store_true", help="show full output with snippets and scores")
    parser.add_argument("--refs", action="store_true", help="show callers and callees for each result (implies --verbose)")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("--dir", default="", dest="directory",
                        metavar="PATH", help="restrict to a directory path substring")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        metavar="KEY=VALUE", help="filter by symbol tag, e.g. visibility=public (repeatable)")
    parser.add_argument("query", help="query text")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    vec = embed(cfg.embedder.endpoint, cfg.embedder.model, args.query)
    blob = to_blob(vec)

    reranker_endpoint = cfg.reranker.endpoint if cfg.reranker.enabled else ""
    reranker_model = cfg.reranker.model if cfg.reranker.enabled else ""
    reranker_api_key = cfg.reranker.api_key if cfg.reranker.enabled else ""
    tag_filter: dict[str, str] = {}
    for t in args.tags:
        if "=" in t:
            k, v = t.split("=", 1)
            tag_filter[k.strip()] = v.strip()
    run_query(conn, blob, args.query, args.n, args.refs, args.exts,
              reranker_endpoint=reranker_endpoint, reranker_model=reranker_model,
              reranker_api_key=reranker_api_key,
              directory=args.directory,
              verbose=args.verbose or args.refs,
              tags=tag_filter or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
