from __future__ import annotations

import argparse
import os
import sys

from blerk import config, db


def _dir_sql(directory: str) -> tuple[str, list[str]]:
    if not directory:
        return "", []
    norm = directory.replace("\\", "/").rstrip("/")
    return "AND (f.path LIKE ? OR f.path LIKE ?)", [f"%{norm}/%", f"%{norm}"]


def _long_functions(conn, directory: str, max_lines: int) -> list[tuple]:
    sql, params = _dir_sql(directory)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line, s.end_line
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.end_line IS NOT NULL
          AND (s.end_line - s.line) > ?
          {sql}
        ORDER BY f.path, s.line
        """,
        (max_lines, *params),
    ).fetchall()
    return [(path, line, "long_function", f"{name} ({end - line} lines)")
            for path, name, line, end in rows]


def _god_files(conn, directory: str, max_symbols: int) -> list[tuple]:
    sql, params = _dir_sql(directory)
    rows = conn.execute(
        f"""
        SELECT f.path, COUNT(*) AS sym_count
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.kind != 'heading'
          {sql}
        GROUP BY f.path
        HAVING sym_count > ?
        ORDER BY sym_count DESC
        """,
        (*params, max_symbols),
    ).fetchall()
    return [(path, 1, "god_file", f"{count} symbols") for path, count in rows]


def _high_fan_out(conn, directory: str, max_callees: int) -> list[tuple]:
    sql, params = _dir_sql(directory)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line,
               COUNT(DISTINCT r.callee_id) + COUNT(DISTINCT e.id) AS total_callees
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN symbol_refs r ON r.caller_id = s.id
        LEFT JOIN external_refs e ON e.caller_id = s.id
        WHERE s.kind IN ('function', 'method')
          {sql}
        GROUP BY s.id
        HAVING total_callees > ?
        ORDER BY f.path, s.line
        """,
        (*params, max_callees),
    ).fetchall()
    return [(path, line, "high_fan_out", f"{name} ({count} callees)")
            for path, name, line, count in rows]


def _unused_symbols(conn, directory: str) -> list[tuple]:
    sql, params = _dir_sql(directory)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN symbol_refs r ON r.callee_id = s.id
        WHERE s.kind IN ('function', 'method')
          AND r.callee_id IS NULL
          {sql}
        ORDER BY f.path, s.line
        """,
        params,
    ).fetchall()
    return [(path, line, "unused_symbol", name) for path, name, line in rows]


def _symbol_count(conn, directory: str) -> int:
    sql, params = _dir_sql(directory)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          {sql}
        """,
        params,
    ).fetchone()
    return row[0] if row else 0


def lint(
    conn,
    directory: str,
    max_lines: int,
    max_callees: int,
    max_symbols: int,
    check_unused: bool,
) -> list[tuple]:
    violations: list[tuple] = []
    violations += _long_functions(conn, directory, max_lines)
    violations += _god_files(conn, directory, max_symbols)
    violations += _high_fan_out(conn, directory, max_callees)
    if check_unused:
        violations += _unused_symbols(conn, directory)
    violations.sort(key=lambda v: (v[0], v[1]))
    return violations


def print_results(directory: str, violations: list[tuple], symbol_count: int) -> None:
    for path, line, rule, display in violations:
        loc = f"{path}:{line}"
        print(f"  {loc:<60} {rule:<22} {display}")
    total = len(violations)
    per100 = round(total * 100.0 / symbol_count, 2) if symbol_count else 0.0
    label = directory or "(all)"
    print(f"\n  {label}  symbols={symbol_count} violations={total} per100s={per100}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint code using the blerk index.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dir", default="", dest="directory", metavar="DIR",
                        help="directory to lint (default: cwd)")
    parser.add_argument("--max-lines", type=int, default=40,
                        help="max lines per function/method (default: 40)")
    parser.add_argument("--max-callees", type=int, default=8,
                        help="max callees per function/method (default: 8)")
    parser.add_argument("--max-symbols", type=int, default=20,
                        help="max symbols per file (default: 20)")
    parser.add_argument("--unused", action="store_true",
                        help="flag functions/methods with no callers in the index")
    args = parser.parse_args(argv)

    directory = args.directory or os.getcwd()
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    violations = lint(conn, directory, args.max_lines, args.max_callees, args.max_symbols, args.unused)
    symbol_count = _symbol_count(conn, directory)
    conn.close()

    print_results(directory, violations, symbol_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
