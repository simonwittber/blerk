from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from blerk import config, db
from blerk_cmd.util import index_root, path_readings, to_relative, to_slash


_MAX_LINES_DEFAULT = 200


def _resolve_file(conn, target: str, root: str = "") -> tuple[str, int, int] | None:
    """Try to resolve target as an indexed file path. Returns (path, start_line, end_line)."""
    norm_target = to_slash(target)
    candidates: list[str] = []

    if os.path.isfile(target):
        candidates.append(os.path.realpath(target).replace("\\", "/"))

    for sql, params in path_readings(target, root, "path"):
        if not sql:
            continue
        rows = conn.execute(f"SELECT path FROM file_paths WHERE 1=1 {sql}", params).fetchall()
        candidates += [r[0] for r in rows]
        if rows:
            break

    if not candidates:
        return None

    # Prefer an exact basename match, then the shortest path.
    def sort_key(path: str) -> tuple[int, int, str]:
        exact = os.path.basename(path) == os.path.basename(norm_target)
        return (0 if exact else 1, len(path), path)

    candidates.sort(key=sort_key)
    return candidates[0], 1, 0


def _resolve_symbol(conn, name: str, path_filter: str = "", root: str = "") -> list[tuple[str, int, int]]:
    def lookup(path_sql: str, path_params: list) -> list[tuple[str, int, int]]:
        rows = conn.execute(
            f"""
            SELECT f.path, s.line, COALESCE(s.end_line, s.line)
            FROM symbols s
            JOIN file_paths f ON f.file_id = s.file_id
            WHERE s.name = ? {path_sql}
            ORDER BY f.path, s.line
            """,
            [name, *path_params],
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    if not path_filter:
        return lookup("", [])

    for sql, params in path_readings(path_filter, root, "f.path"):
        matches = lookup(sql, params)
        if matches:
            return matches
    return []


def _read_lines(path: str, start: int = 1, end: int = 0, max_lines: int = _MAX_LINES_DEFAULT,
                root: str = "") -> str:
    display = to_relative(path, root)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"File not found: {display}"
    except OSError as e:
        return f"Could not read {display}: {e}"

    all_lines = text.splitlines()
    total = len(all_lines)
    if total == 0:
        return ""

    start = max(1, start)
    end = min(end if end >= start else total, total)
    if end < start:
        end = total

    requested_end = end
    if end - start + 1 > max_lines:
        end = start + max_lines - 1

    width = len(str(end))
    lines: list[str] = []
    for i in range(start - 1, end):
        num = i + 1
        lines.append(f"{num:>{width}}  {all_lines[i]}")

    header = f"{display} (lines {start}-{end}/{total})"
    if requested_end > end:
        header += f" [truncated; {requested_end - end} more lines omitted]"

    return header + "\n" + "\n".join(lines)


def show(conn, target: str, *, path_filter: str = "", max_lines: int = _MAX_LINES_DEFAULT,
         root: str = "") -> str:
    if not target:
        return "No target specified."

    # First try as a symbol name.
    symbol_matches = _resolve_symbol(conn, target, path_filter, root)
    if len(symbol_matches) == 1:
        path, start, end = symbol_matches[0]
        return _read_lines(path, start, end, max_lines, root)
    if len(symbol_matches) > 1:
        parts = [f"Multiple symbols named '{target}' found:\n"]
        for path, start, end in symbol_matches:
            parts.append(_read_lines(path, start, end, max_lines, root))
        return "\n\n".join(parts)

    # Fall back to a file path.
    file_match = _resolve_file(conn, target, root)
    if file_match:
        path, start, end = file_match
        return _read_lines(path, start, end, max_lines, root)

    scope = f" under {root}" if root else ""
    return f"No indexed file or symbol matching '{target}'{scope}."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show source code for an indexed file or symbol.")
    parser.add_argument("target", help="file path or exact symbol name")
    parser.add_argument("--file", default="", dest="path_filter", metavar="PATH",
                        help="restrict symbol lookup to this file, relative to the index root or absolute")
    parser.add_argument("--lines", type=int, default=_MAX_LINES_DEFAULT, metavar="N",
                        help=f"maximum number of source lines to display (default { _MAX_LINES_DEFAULT })")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(show(conn, args.target, path_filter=args.path_filter, max_lines=args.lines,
               root=index_root(cfg)))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
