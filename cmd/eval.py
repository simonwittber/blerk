from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from blerk import config, db
from blerk_cmd.llm_describer import describe
from blerk_cmd.query import QueryOptions, embed, query_symbols, to_blob

BASELINE_PATH = Path(__file__).parent / "eval_baseline.json"

_PARAPHRASE_PROMPT = """\
You are generating search queries for a code search engine.
Given a function description, write 2 short natural-language queries (one per line) \
that a developer might type to find this function.
Keep each query under 10 words. Output only the queries, one per line.

Function: {name}({params})
Description: {description}"""


def _paraphrase_queries(llm: config.LLM, name: str, params: str, description: str) -> list[str]:
    prompt = _PARAPHRASE_PROMPT.format(name=name, params=params, description=description)
    try:
        raw = describe(llm.endpoint, llm.model, llm.api_key, prompt)
    except Exception:
        return []
    return [q.strip() for q in raw.splitlines() if q.strip()][:2]


def _find_rank(results, sym_id: int) -> int | None:
    for i, r in enumerate(results):
        if r.id == sym_id:
            return i + 1
    return None


def _run_query(conn, cfg: config.Config, text: str, sym_id: int) -> int | None:
    try:
        vec = embed(cfg.embedder.endpoint, cfg.embedder.model, text)
    except Exception:
        return None
    blob = to_blob(vec)
    opts = QueryOptions(n=20, reranker=cfg.reranker if cfg.reranker.enabled else None)
    results = query_symbols(conn, blob, text, opts)
    return _find_rank(results, sym_id)


def _sample_rows(conn, n_samples: int, pinned_ids: list[int] | None):
    if pinned_ids:
        placeholders = ",".join("?" * len(pinned_ids))
        return conn.execute(
            f"""
            SELECT s.id, s.name, s.kind, COALESCE(s.params, ''), s.description, f.path
            FROM symbols s
            JOIN file_paths f ON f.file_id = s.file_id
            JOIN embeddings e ON e.symbol_id = s.id
            WHERE s.id IN ({placeholders})
              AND s.description IS NOT NULL AND s.description != ''
            """,
            pinned_ids,
        ).fetchall()
    return conn.execute(
        """
        SELECT s.id, s.name, s.kind, COALESCE(s.params, ''), s.description, f.path
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN embeddings e ON e.symbol_id = s.id
        WHERE s.description IS NOT NULL AND s.description != ''
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (n_samples,),
    ).fetchall()


def p_at_1(ranks: list[int | None]) -> float:
    if not ranks:
        return 0.0
    return sum(1 for r in ranks if r == 1) / len(ranks)


def mrr(ranks: list[int | None]) -> float:
    if not ranks:
        return 0.0
    return sum(1.0 / r for r in ranks if r is not None) / len(ranks)


def _fmt_delta(new: float, old: float | None) -> str:
    if old is None:
        return ""
    delta = new - old
    sign = "+" if delta >= 0 else ""
    return f"  (prev: {old:.3f}, {sign}{delta:.3f})"


def _load_baseline() -> dict | None:
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text())
        except Exception:
            return None
    return None


def _save_baseline(
    symbol_ids: list[int],
    b_p1: float, b_mrr: float,
    c_p1: float, c_mrr: float,
) -> None:
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_samples": len(symbol_ids),
        "symbol_ids": symbol_ids,
        "method_B": {"p_at_1": round(b_p1, 4), "mrr": round(b_mrr, 4)},
        "method_C": {"p_at_1": round(c_p1, 4), "mrr": round(c_mrr, 4)},
    }
    BASELINE_PATH.write_text(json.dumps(data, indent=2))


def run_eval(cfg: config.Config, conn, n_samples: int, save: bool) -> None:
    baseline = _load_baseline()
    pinned_ids: list[int] | None = baseline.get("symbol_ids") if baseline else None

    rows = _sample_rows(conn, n_samples, pinned_ids)

    if not rows:
        print("No described and embedded symbols found. Run the describer and embedder first.")
        sys.exit(1)

    reusing = pinned_ids is not None
    print(f"Evaluating {len(rows)} symbols {'(fixed sample from baseline)' if reusing else '(new random sample)'}...")

    llm = cfg.llm[0] if cfg.llm else None

    b_ranks: list[int | None] = []
    b_misses: list[str] = []
    c_ranks: list[int | None] = []
    c_misses: list[str] = []

    for i, (sym_id, name, kind, params, description, path) in enumerate(rows):
        print(f"  [{i+1}/{len(rows)}] {name}", end="\r", flush=True)

        rank_b = _run_query(conn, cfg, description, sym_id)
        b_ranks.append(rank_b)
        if rank_b != 1:
            label = f"rank {rank_b}" if rank_b else "not found"
            b_misses.append(f"{name} [{label}]")

        best_c: int | None = None
        if llm:
            queries = _paraphrase_queries(llm, name, params, description)
            for q in queries:
                r = _run_query(conn, cfg, q, sym_id)
                if r is not None and (best_c is None or r < best_c):
                    best_c = r
        c_ranks.append(best_c)
        if best_c != 1:
            label = f"rank {best_c}" if best_c else "not found"
            c_misses.append(f"{name} [{label}]")

    print()

    prev_b = baseline.get("method_B") if baseline else None
    prev_c = baseline.get("method_C") if baseline else None

    b_p1 = p_at_1(b_ranks)
    b_mrr = mrr(b_ranks)
    c_p1 = p_at_1(c_ranks)
    c_mrr = mrr(c_ranks)

    print("Method B (description-as-query):")
    print(f"  P@1  : {b_p1:.3f}{_fmt_delta(b_p1, prev_b['p_at_1'] if prev_b else None)}")
    print(f"  MRR  : {b_mrr:.3f}{_fmt_delta(b_mrr, prev_b['mrr'] if prev_b else None)}")
    if b_misses:
        print(f"  Misses: {', '.join(b_misses[:10])}" + (" ..." if len(b_misses) > 10 else ""))

    print()
    print("Method C (LLM paraphrase):")
    if not llm:
        print("  (skipped: no LLM configured)")
    else:
        print(f"  P@1  : {c_p1:.3f}{_fmt_delta(c_p1, prev_c['p_at_1'] if prev_c else None)}")
        print(f"  MRR  : {c_mrr:.3f}{_fmt_delta(c_mrr, prev_c['mrr'] if prev_c else None)}")
        if c_misses:
            print(f"  Misses: {', '.join(c_misses[:10])}" + (" ..." if len(c_misses) > 10 else ""))

    if save:
        symbol_ids = [r[0] for r in rows]
        _save_baseline(symbol_ids, b_p1, b_mrr, c_p1, c_mrr)
        print(f"\nSaved baseline to {BASELINE_PATH}")
    else:
        print("\n(--no-save: baseline not updated)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate blerk query ranking quality.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("-n", type=int, default=50, help="number of symbols to sample on first run (default: 50)")
    parser.add_argument("--no-save", action="store_true", help="do not overwrite baseline")
    parser.add_argument("--reset", action="store_true", help="ignore saved symbol IDs and draw a new random sample")
    args = parser.parse_args()

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    if args.reset and BASELINE_PATH.exists():
        BASELINE_PATH.unlink()
        print("Baseline reset. Drawing new random sample.")

    run_eval(cfg, conn, args.n, save=not args.no_save)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
