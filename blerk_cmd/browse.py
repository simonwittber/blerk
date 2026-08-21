from __future__ import annotations

import argparse
import sys
from pathlib import Path

from blerk import config, db
from blerk_cmd.query import _ext_sql, _tag_clause
from blerk_cmd.util import normalize_dir


def _unindexed_subdirs(conn, directory: str) -> list[str]:
    root = Path(normalize_dir(directory))
    if not root.is_dir():
        return []
    norm = normalize_dir(str(root)).rstrip("/")
    unindexed: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        child_norm = normalize_dir(str(child))
        row = conn.execute(
            "SELECT 1 FROM file_paths WHERE path LIKE ? LIMIT 1",
            (f"{child_norm}/%",),
        ).fetchone()
        if row is None:
            unindexed.append(child_norm)
    return unindexed


def browse(
    conn,
    directory: str = "",
    exts: list[str] | None = None,
    symbols: bool = False,
    tags: dict[str, str] | None = None,
) -> str:
    exts = exts or []
    ext_sql, ext_params = _ext_sql(exts)
    tag_sql, tag_params = _tag_clause(tags or {})

    dir_sql = ""
    dir_params: list[str] = []
    if directory:
        norm = normalize_dir(directory).rstrip("/")
        dir_sql = "AND (f.path LIKE ? OR f.path LIKE ?)"
        dir_params = [f"%{norm}/%", f"%{norm}"]

    if not symbols:
        rows = conn.execute(
            f"""
            SELECT DISTINCT f.path
            FROM symbols s
            JOIN file_paths f ON f.file_id = s.file_id
            {tag_sql}
            WHERE s.kind != 'heading'
              {ext_sql} {dir_sql}
            ORDER BY f.path
            """,
            (*tag_params, *ext_params, *dir_params),
        ).fetchall()
        if not rows:
            return "No indexed files found."
        result = "\n".join(r[0] for r in rows)
        unindexed = _unindexed_subdirs(conn, directory)
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
          {ext_sql} {dir_sql}
        ORDER BY f.path, s.line
        """,
        (*tag_params, *ext_params, *dir_params),
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

    result = "\n".join(lines)
    unindexed = _unindexed_subdirs(conn, directory)
    if unindexed:
        result += "\n" + "\n".join(f"[not indexed] {d}" for d in unindexed)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse indexed files and their symbols.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--ext", action="append", default=[], dest="exts",
                        metavar="EXT", help="restrict to file extension, e.g. .py (repeatable)")
    parser.add_argument("directory", help="restrict to this directory")
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

    directory = normalize_dir(args.directory)
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(browse(conn, directory, args.exts, symbols=args.symbols, tags=tag_filter or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
