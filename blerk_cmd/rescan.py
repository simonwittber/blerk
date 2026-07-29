from __future__ import annotations

import argparse
import sys

from blerk import config, db


def rescan(conn, directory: str = "", exts: list[str] | None = None) -> int:
    conditions: list[str] = []
    params: list[str] = []

    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        conditions.append("path LIKE ?")
        params.append(f"{norm}/%")

    for ext in (exts or []):
        conditions.append("path LIKE ?")
        params.append(f"%{ext}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    row = conn.execute(f"SELECT COUNT(*) FROM files {where}", params).fetchone()
    n = int(row[0]) if row else 0
    if n == 0:
        return 0

    conn.execute(
        f"DELETE FROM symbol_queue WHERE file_id IN (SELECT id FROM files {where})",
        params,
    )
    conn.execute(
        f"INSERT INTO symbol_queue(file_id, priority, queued_at) "
        f"SELECT id, 10, unixepoch() FROM files {where}",
        params,
    )
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue files for symbolization.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("path", nargs="?", default="", help="directory to rescan (default: all indexed files)")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .cs (repeatable)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    n = rescan(conn, args.path, args.exts)
    conn.close()

    if n == 0:
        print("No indexed files found.")
        return 1
    print(f"Queued {n} file(s) for re-symbolization.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
