from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
from dataclasses import dataclass

from blerk import config, db
from blerk_cmd.lint_rules import RULES, Violation, build_scope


@dataclass
class _Suppression:
    dir: str
    rules: list[str]


def load_suppressions(directory: str) -> list[_Suppression]:
    result: list[_Suppression] = []
    for root, _dirs, files in os.walk(directory):
        if ".blerk" in files:
            try:
                with open(os.path.join(root, ".blerk"), "rb") as f:
                    data = tomllib.load(f)
                rules = data.get("suppress", [])
                if rules:
                    result.append(_Suppression(
                        dir=root.replace("\\", "/"),
                        rules=rules,
                    ))
            except Exception:
                pass
    return result


def _is_suppressed(path: str, rule: str, suppressions: list[_Suppression]) -> bool:
    p = path.replace("\\", "/")
    for s in suppressions:
        if p == s.dir or p.startswith(s.dir + "/"):
            if "*" in s.rules or rule in s.rules:
                return True
    return False


def _symbol_count(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM symbols s
        JOIN _lint_files f ON f.file_id = s.file_id
        WHERE s.kind IN ('function', 'method')
        """
    ).fetchone()
    return row[0] if row else 0


def lint(conn, directory: str, thresholds: dict[str, int], excludes: list[str] = [],
         timing: bool = False) -> list[Violation]:
    build_scope(conn, directory, excludes)
    suppressions = load_suppressions(directory)
    violations: list[Violation] = []
    for rule in RULES:
        t = thresholds.get(rule.name, rule.default)
        if t < 0:
            continue
        t0 = time.perf_counter()
        result = rule.fn(conn, directory, t, excludes)
        if timing:
            ms = (time.perf_counter() - t0) * 1000
            print(f"  {rule.name:<28} {ms:6.1f}ms  {len(result)} findings", file=sys.stderr)
        violations += result
    violations.sort(key=lambda v: (v[0], v[1]))
    if suppressions:
        violations = [v for v in violations if not _is_suppressed(v[0], v[2], suppressions)]
    return violations


ConfusingSymbol = tuple[str, int, str, str]  # path, line, name, reason


def fetch_confusing(conn) -> list[ConfusingSymbol]:
    rows = conn.execute(
        """
        SELECT f.path, s.line, s.name, COALESCE(sr.value, '')
        FROM symbols s
        JOIN _lint_files f ON f.file_id = s.file_id
        JOIN symbol_tags st ON st.symbol_id = s.id AND st.key = 'confusing' AND st.value = 'true'
        LEFT JOIN symbol_tags sr ON sr.symbol_id = s.id AND sr.key = 'confusing_reason'
        WHERE s.kind IN ('function', 'method')
        ORDER BY f.path, s.line
        """
    ).fetchall()
    return [(path, line, name, reason) for path, line, name, reason in rows]


def print_results(directory: str, violations: list[Violation], symbol_count: int,
                  confusing: list[ConfusingSymbol] | None = None) -> None:
    for path, line, rule, display in violations:
        loc = f"{path}:{line}"
        print(f"  {loc:<60} {rule:<22} {display}")
    total = len(violations)
    per100 = round(total * 100.0 / symbol_count, 2) if symbol_count else 0.0
    label = directory or "(all)"
    confusing_count = len(confusing) if confusing else 0
    print(f"\n  {label}  symbols={symbol_count} violations={total} per100s={per100} confusing={confusing_count}\n")
    if confusing:
        print("  confusing:")
        for path, line, name, reason in confusing:
            loc = f"{path}:{line}"
            suffix = f"  {reason}" if reason else ""
            print(f"    {loc:<60} {name}{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint code using the blerk index.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dir", default="", dest="directory", metavar="DIR",
                        help="directory to lint (default: cwd)")
    parser.add_argument("--exclude", action="append", dest="excludes", default=[], metavar="PATTERN",
                        help="exclude paths matching glob pattern (repeatable)")
    parser.add_argument("--timing", action="store_true", help="print per-rule timing to stderr")

    for rule in RULES:
        if rule.default < 0:
            parser.add_argument(f"--{rule.flag}", action="store_true", help=rule.help)
        else:
            parser.add_argument(f"--{rule.flag}", type=int, default=rule.default,
                                metavar="N", help=rule.help)

    args = parser.parse_args(argv)

    thresholds: dict[str, int] = {}
    for rule in RULES:
        attr = rule.flag.replace("-", "_")
        val = getattr(args, attr)
        thresholds[rule.name] = 0 if (rule.default < 0 and val) else (-1 if rule.default < 0 else val)

    directory = os.path.abspath(args.directory) if args.directory else os.getcwd()
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    violations = lint(conn, directory, thresholds, args.excludes, timing=args.timing)
    symbol_count = _symbol_count(conn)
    confusing = fetch_confusing(conn)
    conn.execute("DROP TABLE IF EXISTS _lint_files")
    conn.close()

    print_results(directory, violations, symbol_count, confusing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
