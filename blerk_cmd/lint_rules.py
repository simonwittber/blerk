from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

Violation = tuple[str, int, str, str]  # path, line, rule, display

RULES: list[Rule] = []


@dataclass
class Rule:
    name: str
    fn: Callable[..., list[Violation]]
    default: int    # threshold; -1 means disabled by default (opt-in)
    flag: str       # argparse long flag, e.g. "max-lines"
    help: str


def rule(default: int, flag: str, help: str):
    def decorator(fn: Callable) -> Callable:
        RULES.append(Rule(name=fn.__name__, fn=fn, default=default, flag=flag, help=help))
        return fn
    return decorator


def _path_clauses(directory: str, excludes: list[str]) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    if directory:
        norm = directory.replace("\\", "/").rstrip("/")
        parts.append("(f.path LIKE ? OR f.path LIKE ?)")
        params += [f"%{norm}/%", f"%{norm}"]
    for pat in excludes:
        sql = pat.replace("\\", "/").replace("*", "%").replace("?", "_")
        parts.append("f.path NOT LIKE ?")
        params.append(sql)
    clause = ("AND " + " AND ".join(parts)) if parts else ""
    return clause, params


@rule(default=40, flag="max-lines", help="max lines per function/method (default: 40)")
def long_function(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line, s.end_line
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.end_line IS NOT NULL
          AND (s.end_line - s.line) > ?
          {clause}
        ORDER BY f.path, s.line
        """,
        (threshold, *params),
    ).fetchall()
    return [(path, line, "long_function", f"{name} ({end - line} lines)")
            for path, name, line, end in rows]


@rule(default=20, flag="max-symbols", help="max symbols per file (default: 20)")
def god_file(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, COUNT(*) AS sym_count
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind != 'heading'
          {clause}
        GROUP BY f.path
        HAVING sym_count > ?
        ORDER BY sym_count DESC
        """,
        (*params, threshold),
    ).fetchall()
    return [(path, 1, "god_file", f"{count} symbols") for path, count in rows]


@rule(default=8, flag="max-callees", help="max callees per function/method (default: 8)")
def high_fan_out(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line,
               COUNT(DISTINCT r.callee_id) + COUNT(DISTINCT e.id) AS total_callees
        FROM symbols s JOIN files f ON f.id = s.file_id
        LEFT JOIN symbol_refs r ON r.caller_id = s.id
        LEFT JOIN external_refs e ON e.caller_id = s.id
        WHERE s.kind IN ('function', 'method')
          {clause}
        GROUP BY s.id
        HAVING total_callees > ?
        ORDER BY f.path, s.line
        """,
        (*params, threshold),
    ).fetchall()
    return [(path, line, "high_fan_out", f"{name} ({count} callees)")
            for path, name, line, count in rows]


@rule(default=4, flag="max-params", help="max parameters per function/method (default: 4)")
def too_many_params(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line, s.param_count
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.param_count > ?
          {clause}
        ORDER BY f.path, s.line
        """,
        (threshold, *params),
    ).fetchall()
    return [(path, line, "too_many_params", f"{name} ({count} params)")
            for path, name, line, count in rows]


@rule(default=3, flag="max-nesting", help="max nesting depth (default: 3)")
def deep_nesting(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line, s.nesting_depth
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.nesting_depth > ?
          {clause}
        ORDER BY f.path, s.line
        """,
        (threshold, *params),
    ).fetchall()
    return [(path, line, "deep_nesting", f"{name} (depth {depth})")
            for path, name, line, depth in rows]


@rule(default=-1, flag="unused", help="flag functions/methods with no callers (opt-in)")
def unused_symbol(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line
        FROM symbols s JOIN files f ON f.id = s.file_id
        LEFT JOIN symbol_refs r ON r.callee_id = s.id
        WHERE s.kind IN ('function', 'method')
          AND r.callee_id IS NULL
          {clause}
        ORDER BY f.path, s.line
        """,
        params,
    ).fetchall()
    return [(path, line, "unused_symbol", name) for path, name, line in rows]


@rule(default=-1, flag="statics", help="flag static symbols (opt-in)")
def static_symbol(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    clause, params = _path_clauses(directory, excludes)
    rows = conn.execute(
        f"""
        SELECT f.path, s.name, s.line, s.kind
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.is_static = 1
          {clause}
        ORDER BY f.path, s.line
        """,
        params,
    ).fetchall()
    return [(path, line, "static_symbol", f"{name} ({kind})")
            for path, name, line, kind in rows]
