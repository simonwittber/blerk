from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.util import Scope, index_root, scope_clause, to_relative


def deps(conn, directory: str = "", root: str = "") -> str:
    scope_sql, scope_params = scope_clause(Scope(directory=directory, root=root), "f_caller.path")

    rows = conn.execute(
        f"""
        SELECT DISTINCT f_caller.path, f_callee.path
        FROM symbol_refs r
        JOIN symbols s_caller ON s_caller.id = r.caller_id
        JOIN symbols s_callee ON s_callee.id = r.callee_id
        JOIN file_paths f_caller ON f_caller.file_id = s_caller.file_id
        JOIN file_paths f_callee ON f_callee.file_id = s_callee.file_id
        WHERE f_caller.path != f_callee.path
          {scope_sql}
        ORDER BY f_caller.path, f_callee.path
        """,
        scope_params,
    ).fetchall()

    if not rows:
        where = directory or (root or "the index")
        return (
            f"No dependency data in {where}."
            " Ensure engine=treesitter and symbol_refs are populated."
        )

    # Group callees by caller file.
    graph: dict[str, list[str]] = {}
    for caller_path, callee_path in rows:
        caller = to_relative(caller_path, root)
        graph.setdefault(caller, []).append(to_relative(callee_path, root))

    lines = [f"{caller} -> {', '.join(callees)}" for caller, callees in sorted(graph.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show file-level dependency graph.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("directory", nargs="?", default="",
                        help="restrict to this directory, relative to the index root or absolute (default: the whole root)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(deps(conn, args.directory, index_root(cfg)))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
