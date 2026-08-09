from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from blerk import config, db
from blerk_cmd.util import normalize_dir


_MAX_LINES_DEFAULT = 200


def _resolve_file(conn, target: str) -> tuple[str, int, int] | None:
    """Try to resolve target as an indexed file path. Returns (path, start_line, end_line)."""
    norm_target = normalize_dir(target)
    candidates: list[tuple[str, int]] = []

    if os.path.isfile(target):
        real = os.path.realpath(target).replace("\\", "/")
        candidates.append((real, 1))
        candidates.append((target.replace("\\", "/"), 1))

    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE ? OR path LIKE ?",
        (f"%{norm_target}", f"%{norm_target}/%"),
    ).fetchall()
    for (path,) in rows:
        if path.endswith(norm_target):
            candidates.append((path, 1))

    if not candidates:
        return None

    # Prefer exact basename match, then shortest path.
    def sort_key(item: tuple[str, int]) -> tuple[int, int, str]:
        path = item[0]
        name = os.path.basename(path)
        exact = name == os.path.basename(norm_target)
        return (0 if exact else 1, len(path), path)

    candidates.sort(key=sort_key)
    path, start_line = candidates[0]
    return path, start_line, 0


def _resolve_symbol(conn, name: str, path_filter: str = "") -> list[tuple[str, int, int]]:
    params: list = [name]
    path_sql = ""
    if path_filter:
        norm = normalize_dir(path_filter)
        path_sql = "AND f.path LIKE ?"
        params.append(f"%{norm}%")

    rows = conn.execute(
        f"""
        SELECT f.path, s.line, COALESCE(s.end_line, s.line)
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.name = ? {path_sql}
        ORDER BY f.path, s.line
        """,
        params,
    ).fetchall()

    return [(r[0], r[1], r[2]) for r in rows]


def _read_lines(path: str, start: int = 1, end: int = 0, max_lines: int = _MAX_LINES_DEFAULT) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"File not found: {path}"
    except OSError as e:
        return f"Could not read {path}: {e}"

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

    header = f"{path} (lines {start}-{end}/{total})"
    if requested_end > end:
        header += f" [truncated; {requested_end - end} more lines omitted]"

    return header + "\n" + "\n".join(lines)


def show(conn, target: str, *, path_filter: str = "", max_lines: int = _MAX_LINES_DEFAULT) -> str:
    if not target:
        return "No target specified."

    # First try as a symbol name.
    symbol_matches = _resolve_symbol(conn, target, path_filter)
    if len(symbol_matches) == 1:
        path, start, end = symbol_matches[0]
        return _read_lines(path, start, end, max_lines)
    if len(symbol_matches) > 1:
        parts = [f"Multiple symbols named '{target}' found:\n"]
        for path, start, end in symbol_matches:
            parts.append(_read_lines(path, start, end, max_lines))
        return "\n\n".join(parts)

    # Fall back to a file path.
    file_match = _resolve_file(conn, target)
    if file_match:
        path, start, end = file_match
        return _read_lines(path, start, end, max_lines)

    return f"No indexed file or symbol matching '{target}' found."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show source code for an indexed file or symbol.")
    parser.add_argument("target", help="file path or exact symbol name")
    parser.add_argument("--file", default="", dest="path_filter", metavar="PATH",
                        help="restrict symbol lookup to a file path substring")
    parser.add_argument("--lines", type=int, default=_MAX_LINES_DEFAULT, metavar="N",
                        help=f"maximum number of source lines to display (default { _MAX_LINES_DEFAULT })")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(show(conn, args.target, path_filter=args.path_filter, max_lines=args.lines))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
