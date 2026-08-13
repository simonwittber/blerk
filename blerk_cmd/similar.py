from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from blerk import config, db
from blerk_cmd.query import _ext_sql
from blerk_cmd.util import Scope, build_path_filters


def similar(conn, directory: str, threshold: float, exts: list[str] | None = None, top_k: int = 20) -> None:
    """Find semantically similar code blocks within a scoped directory."""

    # Build WHERE clause for directory and extension filters
    ext_clause, ext_params = _ext_sql(exts or [])
    scope = Scope(directory=directory, exts=[])
    dir_filters, dir_params = build_path_filters(scope)
    dir_clause = ("AND " + " AND ".join(dir_filters)) if dir_filters else ""

    where_fragments = [
        "AND cb.block_index = 0",
        "AND s.kind IN ('function', 'method')",
        "AND f.path NOT LIKE '%test%'",
        ext_clause,
        dir_clause,
    ]
    where = "WHERE 1=1 " + " ".join(f for f in where_fragments if f)
    where_params = ext_params + dir_params

    # Fetch all blocks in scope with embeddings
    all_blocks = conn.execute(
        f"""
        SELECT s.id, s.name, f.path, s.line, e.vector, e.model
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN code_blocks cb ON cb.symbol_id = s.id
        JOIN embeddings e ON e.content_hash = cb.content_hash
        {where}
        ORDER BY s.id
        """,
        where_params,
    ).fetchall()

    if not all_blocks:
        print("No indexed blocks found in this scope.")
        return

    # Determine the model to use (prefer the most recent one if multiple exist)
    model_counts = {}
    for _, _, _, _, _, m in all_blocks:
        model_counts[m] = model_counts.get(m, 0) + 1

    if not model_counts:
        print("No embeddings found.")
        return

    model = max(model_counts, key=model_counts.get)

    # Filter to blocks with the chosen model
    all_blocks = [(sid, name, path, line, vec, m) for sid, name, path, line, vec, m in all_blocks if m == model]

    if not all_blocks:
        print(f"No blocks with embedding model '{model}' found.")
        return

    print(f"Scanning {len(all_blocks)} blocks with model '{model}'...\n")
    sym_metadata = {sid: (name, path, line) for sid, name, path, line, _, _ in all_blocks}

    # Build edges via per-block nearest neighbour query
    edges_dict: dict[tuple[int, int], float] = {}
    match_count = 0

    for i, (sym_id, name, path, line, vector, _) in enumerate(all_blocks, 1):
        if i % max(1, len(all_blocks) // 20) == 0:
            print(f"[{i}/{len(all_blocks)}]", file=sys.stderr, flush=True)

        similar_rows = conn.execute(
            f"""
            SELECT s.id, vec_distance_cosine(e.vector, ?) AS dist
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            JOIN code_blocks cb ON cb.symbol_id = s.id
            JOIN embeddings e ON e.content_hash = cb.content_hash
            {where}
              AND s.id != ?
              AND e.model = ?
              AND vec_distance_cosine(e.vector, ?) < ?
            ORDER BY dist ASC
            LIMIT ?
            """,
            [vector] + where_params + [sym_id, model, vector, threshold, top_k],
        ).fetchall()

        if similar_rows:
            for other_sym_id, dist in similar_rows:
                match_count += 1
                pair = (min(sym_id, other_sym_id), max(sym_id, other_sym_id))
                if pair not in edges_dict or dist < edges_dict[pair]:
                    edges_dict[pair] = dist

    if match_count == 0:
        print(f"No similar blocks found (threshold={threshold}).")
        return

    print(f"\nFound {match_count} match(es). Grouping into clusters...\n")

    edges = [(a, b, d) for (a, b), d in edges_dict.items()]

    # Union-find to group into components
    all_syms = {sym_id for sym_id, _, _, _, _, _ in all_blocks}
    parent: dict[int, int] = {sid: sid for sid in all_syms}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Group nodes by component
    components: dict[int, list[int]] = defaultdict(list)
    for sym_id in all_syms:
        root = find(sym_id)
        components[root].append(sym_id)

    # Print grouped clusters (components with >1 member)
    group_num = 1
    has_groups = False
    for root, member_ids in sorted(components.items()):
        if len(member_ids) > 1:
            has_groups = True
            member_set = set(member_ids)
            member_edges = [(a, b, d) for a, b, d in edges if a in member_set and b in member_set]

            print(f"=== cluster {group_num} ({len(member_ids)} members) ===")

            # Print members with their nearest distance to any other member
            for sym_id in sorted(member_ids):
                min_dist = float("inf")
                for a, b, d in member_edges:
                    if a == sym_id or b == sym_id:
                        min_dist = min(min_dist, d)
                if min_dist == float("inf"):
                    min_dist = 0.0

                name, path, line = sym_metadata[sym_id]
                print(f"  {min_dist:.2f}  {path}:{line}  {name}")
            print()

            group_num += 1

    if not has_groups:
        print("(No multi-member clusters found after grouping.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find semantically similar code blocks.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.1,
        help="cosine distance threshold (0=identical, 1=orthogonal, default: 0.1)",
    )
    parser.add_argument(
        "--ext",
        action="append",
        default=[],
        dest="exts",
        metavar="EXT",
        help="restrict to file extension, e.g. .py (repeatable)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="max neighbours to scan per block (default: 20)",
    )
    parser.add_argument("directory", help="directory to search within")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    try:
        similar(conn, args.directory, args.threshold, args.exts, args.top_k)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
