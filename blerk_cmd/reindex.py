from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.util import normalize_dir


def reindex_embeddings(conn, directory: str = "", exts: list[str] | None = None) -> int:
    """Re-queue code blocks for re-embedding."""
    conditions: list[str] = []
    params: list[str] = []

    if directory:
        norm = normalize_dir(directory).rstrip("/")
        conditions.append("(f.path LIKE ? OR f.path LIKE ?)")
        params += [f"%{norm}/%", f"%{norm}"]

    for ext in (exts or []):
        conditions.append("f.path LIKE ?")
        params.append(f"%{ext}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Count blocks that will be re-indexed
    row = conn.execute(
        f"SELECT COUNT(DISTINCT cb.id) FROM code_blocks cb "
        f"JOIN symbols s ON s.id = cb.symbol_id "
        f"JOIN files f ON f.id = s.file_id {where}",
        params
    ).fetchone()
    n = int(row[0]) if row else 0
    if n == 0:
        return 0

    # Delete existing queue entries for these blocks
    conn.execute(
        f"DELETE FROM code_block_embed_queue WHERE block_id IN ("
        f"  SELECT cb.id FROM code_blocks cb "
        f"  JOIN symbols s ON s.id = cb.symbol_id "
        f"  JOIN files f ON f.id = s.file_id {where}"
        f")",
        params,
    )

    # Insert new queue entries with priority 2 (higher than fresh indexes)
    conn.execute(
        f"INSERT INTO code_block_embed_queue(block_id, priority, queued_at) "
        f"SELECT cb.id, 2, unixepoch() FROM code_blocks cb "
        f"JOIN symbols s ON s.id = cb.symbol_id "
        f"JOIN files f ON f.id = s.file_id {where}",
        params,
    )
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue code blocks for re-embedding.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--all", action="store_true", help="re-index all blocks")
    parser.add_argument("path", nargs="?", default="", help="directory to reindex (or use --all)")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .cs (repeatable)")
    args = parser.parse_args(argv)

    if not args.all and not args.path:
        parser.error("Specify a path or use --all")

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    if args.all:
        # Re-queue everything
        conn.execute("DELETE FROM code_block_embed_queue")
        row = conn.execute("SELECT COUNT(*) FROM code_blocks").fetchone()
        n = int(row[0]) if row else 0
        if n > 0:
            conn.execute(
                "INSERT INTO code_block_embed_queue(block_id, priority, queued_at) "
                "SELECT id, 1, unixepoch() FROM code_blocks"
            )
    else:
        n = reindex_embeddings(conn, args.path, args.exts)

    conn.commit()
    conn.close()

    if n == 0:
        print("No blocks found.")
        return 1
    print(f"Queued {n} block(s) for re-embedding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
