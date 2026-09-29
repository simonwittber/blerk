from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.util import Scope, index_root, scope_filters

# file_paths carries the path column, so the join goes through file_id rather than through files.
_BLOCKS_IN_SCOPE = (
    "FROM code_blocks cb "
    "JOIN symbols s ON s.id = cb.symbol_id "
    "JOIN file_paths f ON f.file_id = s.file_id "
)


def reindex_embeddings(conn, directory: str = "", exts: list[str] | None = None, root: str = "") -> int:
    """Re-queue code blocks for re-embedding."""
    conditions, params = scope_filters(Scope(directory=directory, exts=exts or [], root=root))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Count blocks that will be re-indexed
    row = conn.execute(
        f"SELECT COUNT(DISTINCT cb.id) {_BLOCKS_IN_SCOPE}{where}", params
    ).fetchone()
    n = int(row[0]) if row else 0
    if n == 0:
        return 0

    # Delete existing queue entries for these blocks
    conn.execute(
        f"DELETE FROM code_block_embed_queue WHERE block_id IN ("
        f"  SELECT cb.id {_BLOCKS_IN_SCOPE}{where})",
        params,
    )

    # Insert new queue entries with priority 2 (higher than fresh indexes)
    conn.execute(
        f"INSERT INTO code_block_embed_queue(block_id, priority, queued_at) "
        f"SELECT cb.id, 2, unixepoch() {_BLOCKS_IN_SCOPE}{where}",
        params,
    )
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue code blocks for re-embedding.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--all", action="store_true", help="re-index all blocks")
    parser.add_argument("path", nargs="?", default="",
                        help="directory to reindex, relative to the index root or absolute (or use --all)")
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
        n = reindex_embeddings(conn, args.path, args.exts, index_root(cfg))

    conn.commit()
    conn.close()

    if n == 0:
        print("No blocks found.")
        return 1
    print(f"Queued {n} block(s) for re-embedding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
