from __future__ import annotations

import argparse
import sys

from blerk import config, db, ignore_match


def purge(conn, ignore_file: str, dry_run: bool = False) -> int:
    patterns = ignore_match.load_ignore_file(ignore_file)
    ignore_set = ignore_match.IgnoreSet(dir="/", patterns=patterns)

    rows = conn.execute("SELECT id, path FROM files").fetchall()
    to_delete: list[int] = []
    for file_id, path in rows:
        norm = path.replace("\\", "/")
        if ignore_match.is_ignored(norm, is_dir=False, sets=[ignore_set]):
            to_delete.append(file_id)
            if dry_run:
                print(path)

    if not to_delete:
        return 0

    if not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", to_delete)
        conn.commit()

    return len(to_delete)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove indexed files that match ignore patterns.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dry-run", action="store_true", help="Print matching paths without deleting")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    ignore_file = cfg.watch.ignore_file
    if not ignore_file:
        print("purge: no ignore_file configured")
        return 1

    conn = db.open_db(cfg.db.path)
    n = purge(conn, ignore_file, dry_run=args.dry_run)
    conn.close()

    if args.dry_run:
        print(f"\n{n} file(s) would be removed.")
    else:
        print(f"Removed {n} file(s) from the index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
