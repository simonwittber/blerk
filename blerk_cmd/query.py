from __future__ import annotations

import argparse
import sqlite3
import struct
import sys

import httpx

from blerk import config, db

_RRF_K = 60
_OVERFETCH = 5


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


def _heading_clause(exts: list[str]) -> str:
    return "" if ".md" in exts else "AND s.kind != 'heading'"


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


def _vector_ranks(conn, blob: bytes, k: int, exts: list[str]) -> dict[int, int]:
    ext_sql, ext_params = _ext_clause(exts)
    heading_sql = _heading_clause(exts)
    rows = conn.execute(
        f"""
        SELECT s.id
        FROM embeddings e
        JOIN symbols s ON s.id = e.symbol_id
        JOIN files f ON f.id = s.file_id
        WHERE 1=1 {heading_sql} {ext_sql}
        ORDER BY vec_distance_cosine(e.vector, ?) ASC
        LIMIT ?
        """,
        (*ext_params, blob, k),
    ).fetchall()
    return {row[0]: rank for rank, row in enumerate(rows)}


def _bm25_ranks(conn, query_text: str, k: int, exts: list[str]) -> dict[int, int]:
    if not query_text.strip():
        return {}
    ext_sql, ext_params = _ext_clause(exts)
    heading_sql = _heading_clause(exts)
    try:
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.rowid
            JOIN files f ON f.id = s.file_id
            WHERE symbols_fts MATCH ?
              {heading_sql} {ext_sql}
            ORDER BY symbols_fts.rank
            LIMIT ?
            """,
            (query_text, *ext_params, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: rank for rank, row in enumerate(rows)}


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def run_query(
    conn,
    blob: bytes,
    query_text: str,
    n: int,
    refs: bool,
    exts: list[str] | None = None,
) -> None:
    k = n * _OVERFETCH
    exts = exts or []

    vector = _vector_ranks(conn, blob, k, exts)
    bm25 = _bm25_ranks(conn, query_text, k, exts)

    all_ids = set(vector) | set(bm25)
    scores: dict[int, float] = {}
    for id_ in all_ids:
        score = 0.0
        if id_ in vector:
            score += _rrf_score(vector[id_])
        if id_ in bm25:
            score += _rrf_score(bm25[id_])
        scores[id_] = score

    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:n]
    if not top_ids:
        return

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
            COALESCE(s.snippet, '')
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.id IN ({placeholders})
        """,
        top_ids,
    ).fetchall()

    rows.sort(key=lambda r: scores[r[0]], reverse=True)

    for i, (id_, name, kind, path, line, end_line, desc, snippet) in enumerate(rows):
        score = scores[id_]
        print(f"[{i + 1}] {kind} {name}")
        print(f"path: {path}")
        print(f"lines: {line}-{end_line}")
        print(f"score: {score:.3f}")
        if desc:
            print(f"description: {desc}")
        if snippet:
            print(f"snippet:\n{snippet}")
        if refs:
            print_refs(conn, id_)
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("-n", type=int, default=10, help="number of results")
    parser.add_argument("--refs", action="store_true", help="show callers and callees for each result")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("query", help="query text")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    vec = embed(cfg.embedder.endpoint, cfg.embedder.model, args.query)
    blob = to_blob(vec)

    run_query(conn, blob, args.query, args.n, args.refs, args.exts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
