from __future__ import annotations

import argparse
import sys

from blerk import config, db


def rescan(conn, directory: str = "") -> int:
    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        file_sql = "WHERE path LIKE ?"
        file_params: list[str] = [f"{norm}/%"]
    else:
        file_sql = ""
        file_params = []

    row = conn.execute(f"SELECT COUNT(*) FROM files {file_sql}", file_params).fetchone()
    n = int(row[0]) if row else 0
    if n == 0:
        return 0

    conn.execute(
        f"DELETE FROM symbol_queue WHERE file_id IN (SELECT id FROM files {file_sql})",
        file_params,
    )
    conn.execute(
        f"INSERT INTO symbol_queue(file_id, priority, queued_at) "
        f"SELECT id, 10, unixepoch() FROM files {file_sql}",
        file_params,
    )
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue files for symbolization.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("path", nargs="?", default="", help="directory to rescan (default: all indexed files)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    n = rescan(conn, args.path)
    conn.close()

    if n == 0:
        print("No indexed files found.")
        return 1
    print(f"Queued {n} file(s) for re-symbolization.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
