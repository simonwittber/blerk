from __future__ import annotations

import argparse
import os
import sys

from blerk import config, db
from blerk_cmd.query import _ext_clause


def browse(
    conn,
    directory: str = "",
    exts: list[str] | None = None,
) -> str:
    exts = exts or []
    ext_sql, ext_params = _ext_clause(exts)

    dir_sql = ""
    dir_params: list[str] = []
    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        dir_sql = "AND (f.path LIKE ? OR f.path LIKE ?)"
        dir_params = [f"%{norm}/%", f"%{norm}"]

    rows = conn.execute(
        f"""
        SELECT f.path, s.kind, s.name, s.line, s.end_line, COALESCE(s.params, '')
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.kind != 'heading'
          {ext_sql} {dir_sql}
        ORDER BY f.path, s.line
        """,
        (*ext_params, *dir_params),
    ).fetchall()

    if not rows:
        return "No indexed symbols found."

    lines: list[str] = []
    current_path = None
    # stack of (end_line, indent) for enclosing containers
    containers: list[tuple[int, int]] = []

    for path, kind, name, line, end_line, params in rows:
        if path != current_path:
            if current_path is not None:
                lines.append("")
            lines.append(path)
            current_path = path
            containers = []

        end = end_line or line

        # pop containers that ended before this symbol
        while containers and containers[-1][0] < line:
            containers.pop()

        depth = len(containers)
        indent = "  " + "  " * depth

        sig = f"({params})" if params else ""
        lines.append(f"{indent}{kind} {name}{sig} ({line}-{end})")

        # push this symbol if it can contain others
        if kind in ("class", "struct", "interface", "enum", "type") and end > line:
            containers.append((end, depth + 1))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse indexed files and their symbols.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("--dir", default="", dest="directory",
                        metavar="DIR", help="restrict to directory (default: cwd)")
    args = parser.parse_args(argv)

    directory = args.directory or os.getcwd()
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(browse(conn, directory, args.exts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
