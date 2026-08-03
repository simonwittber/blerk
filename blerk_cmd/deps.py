from __future__ import annotations

import argparse
import os
import sys

from blerk import config, db


def deps(conn, directory: str = "") -> str:
    dir_sql = ""
    dir_params: list[str] = []
    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        dir_sql = "AND f_caller.path LIKE ?"
        dir_params = [f"%{norm}/%"]

    rows = conn.execute(
        f"""
        SELECT DISTINCT f_caller.path, f_callee.path
        FROM symbol_refs r
        JOIN symbols s_caller ON s_caller.id = r.caller_id
        JOIN symbols s_callee ON s_callee.id = r.callee_id
        JOIN files f_caller ON f_caller.id = s_caller.file_id
        JOIN files f_callee ON f_callee.id = s_callee.file_id
        WHERE f_caller.path != f_callee.path
          {dir_sql}
        ORDER BY f_caller.path, f_callee.path
        """,
        dir_params,
    ).fetchall()

    if not rows:
        return "No dependency data found. Ensure engine=treesitter and symbol_refs are populated."

    # Strip common directory prefix for compact display.
    prefix = ""
    if directory:
        prefix = directory.replace("\\", "/").rstrip("/") + "/"

    def rel(path: str) -> str:
        p = path.replace("\\", "/")
        return p[len(prefix):] if prefix and p.startswith(prefix) else p

    # Group callees by caller file.
    graph: dict[str, list[str]] = {}
    for caller_path, callee_path in rows:
        c = rel(caller_path)
        graph.setdefault(c, []).append(rel(callee_path))

    lines = [f"{caller} -> {', '.join(callees)}" for caller, callees in sorted(graph.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show file-level dependency graph.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dir", default="", dest="directory",
                        metavar="DIR", help="restrict to directory (default: cwd)")
    args = parser.parse_args(argv)

    directory = os.path.realpath(args.directory or ".")
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(deps(conn, directory))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
