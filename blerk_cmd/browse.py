from __future__ import annotations

import argparse
import sys
from pathlib import Path

from blerk import config, db
from blerk_cmd.query import _tag_clause
from blerk_cmd.util import Scope, index_root, scope_clause, scope_directory, to_relative


def _unindexed_subdirs(conn, directory: str, root: str) -> list[str]:
    start = Path(scope_directory(Scope(directory=directory, root=root)))
    if not start.is_dir():
        return []
    unindexed: list[str] = []
    for child in sorted(start.iterdir()):
        if not child.is_dir():
            continue
        child_scope = Scope(directory=str(child))
        filters, params = scope_clause(child_scope, "path")
        row = conn.execute(f"SELECT 1 FROM file_paths WHERE 1=1 {filters} LIMIT 1", params).fetchone()
        if row is None:
            unindexed.append(to_relative(str(child).replace("\\", "/"), root))
    return unindexed


def browse(
    conn,
    directory: str = "",
    exts: list[str] | None = None,
    symbols: bool = False,
    tags: dict[str, str] | None = None,
    root: str = "",
) -> str:
    scope = Scope(directory=directory, exts=exts or [], root=root)
    scope_sql, scope_params = scope_clause(scope)
    tag_sql, tag_params = _tag_clause(tags or {})

    if not symbols:
        rows = conn.execute(
            f"""
            SELECT DISTINCT f.path
            FROM symbols s
            JOIN file_paths f ON f.file_id = s.file_id
            {tag_sql}
            WHERE s.kind != 'heading'
              {scope_sql}
            ORDER BY f.path
            """,
            (*tag_params, *scope_params),
        ).fetchall()
        if not rows:
            return _nothing_found(directory, root, "files")
        result = "\n".join(to_relative(r[0], root) for r in rows)
        unindexed = _unindexed_subdirs(conn, directory, root)
        if unindexed:
            result += "\n" + "\n".join(f"[not indexed] {d}" for d in unindexed)
        return result

    rows = conn.execute(
        f"""
        SELECT f.path, s.kind, s.name, s.line, s.end_line, COALESCE(s.params, '')
        FROM symbols s
        JOIN file_paths f ON f.file_id = s.file_id
        {tag_sql}
        WHERE s.kind != 'heading'
          {scope_sql}
        ORDER BY f.path, s.line
        """,
        (*tag_params, *scope_params),
    ).fetchall()

    if not rows:
        return _nothing_found(directory, root, "symbols")

    lines: list[str] = []
    current_path = None
    # stack of (end_line, indent) for enclosing containers
    containers: list[tuple[int, int]] = []

    for path, kind, name, line, end_line, params in rows:
        if path != current_path:
            if current_path is not None:
                lines.append("")
            lines.append(to_relative(path, root))
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

    result = "\n".join(lines)
    unindexed = _unindexed_subdirs(conn, directory, root)
    if unindexed:
        result += "\n" + "\n".join(f"[not indexed] {d}" for d in unindexed)
    return result


def _nothing_found(directory: str, root: str, what: str) -> str:
    """Name the scope that was searched, so a caller can tell a wrong path from an empty directory."""
    where = directory or (root or "the index")
    line = f"No indexed {what} in {where}."
    if root:
        line += f" Index root: {root}."
    return line


def roots(cfg: config.Config) -> str:
    """List the watch folders that make up the index."""
    if not cfg.watch.folders:
        return "No watch folders configured."
    return "Index roots:\n" + "\n".join(f"  {f}" for f in cfg.watch.folders)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse indexed files and their symbols.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("directory", nargs="?", default="",
                        help="restrict to this directory, relative to the index root or absolute (default: the whole root)")
    parser.add_argument("--roots", action="store_true", help="list the index roots and exit")
    parser.add_argument("--symbols", action="store_true",
                        help="show the full indented symbol tree instead of filenames only")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        metavar="KEY=VALUE", help="filter by symbol tag, e.g. visibility=public (repeatable)")
    args = parser.parse_args(argv)

    tag_filter: dict[str, str] = {}
    for t in args.tags:
        if "=" in t:
            k, v = t.split("=", 1)
            tag_filter[k.strip()] = v.strip()

    cfg = config.load(args.config)
    if args.roots:
        print(roots(cfg))
        return 0

    root = index_root(cfg)
    conn = db.open_db(cfg.db.path)
    print(browse(conn, args.directory, args.exts, symbols=args.symbols, tags=tag_filter or None, root=root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
