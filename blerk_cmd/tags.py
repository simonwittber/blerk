from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.query import _ext_clause


def list_tags(conn, directory: str = "", exts: list[str] | None = None) -> str:
    ext_sql, ext_params = _ext_clause(exts or [])

    dir_sql = ""
    dir_params: list[str] = []
    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        dir_sql = "AND (f.path LIKE ? OR f.path LIKE ?)"
        dir_params = [f"%{norm}/%", f"%{norm}"]

    rows = conn.execute(
        f"""
        SELECT DISTINCT t.key, t.value
        FROM symbol_tags t
        JOIN symbols s ON s.id = t.symbol_id
        JOIN files f ON f.id = s.file_id
        WHERE 1=1 {ext_sql} {dir_sql}
        ORDER BY t.key, t.value
        """,
        (*ext_params, *dir_params),
    ).fetchall()

    if not rows:
        return "No tags found."

    lines: list[str] = []
    current_key = None
    for key, value in rows:
        if key != current_key:
            lines.append(key)
            current_key = key
        lines.append(f"  {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List all tag keys and values in the index.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dir", default="", dest="directory",
                        metavar="DIR", help="restrict to a directory")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .cs (repeatable)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(list_tags(conn, args.directory, args.exts))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
