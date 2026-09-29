from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.util import Scope, index_root, scope_clause


def list_tags(conn, directory: str = "", exts: list[str] | None = None, root: str = "") -> str:
    scope_sql, scope_params = scope_clause(Scope(directory=directory, exts=exts or [], root=root))

    rows = conn.execute(
        f"""
        SELECT DISTINCT t.key, t.value
        FROM symbol_tags t
        JOIN symbols s ON s.id = t.symbol_id
        JOIN file_paths f ON f.file_id = s.file_id
        WHERE 1=1 {scope_sql}
        ORDER BY t.key, t.value
        """,
        scope_params,
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
    parser.add_argument("directory", nargs="?", default="",
                        help="restrict to this directory, relative to the index root or absolute (default: the whole root)")
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .cs (repeatable)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(list_tags(conn, args.directory, args.exts, index_root(cfg)))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
