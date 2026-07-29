from __future__ import annotations

import argparse
import os
import sys

from blerk import config, db
from blerk_cmd.lint_rules import RULES, Violation


def _symbol_count(conn, directory: str) -> int:
    norm = directory.replace("\\", "/").rstrip("/")
    row = conn.execute(
        """
        SELECT COUNT(*) FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND (f.path LIKE ? OR f.path LIKE ?)
        """,
        (f"%{norm}/%", f"%{norm}"),
    ).fetchone()
    return row[0] if row else 0


def lint(conn, directory: str, thresholds: dict[str, int]) -> list[Violation]:
    violations: list[Violation] = []
    for rule in RULES:
        t = thresholds.get(rule.name, rule.default)
        if t < 0:
            continue
        violations += rule.fn(conn, directory, t)
    violations.sort(key=lambda v: (v[0], v[1]))
    return violations


def print_results(directory: str, violations: list[Violation], symbol_count: int) -> None:
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

    directory = args.directory or os.getcwd()
    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    violations = lint(conn, directory, thresholds)
    symbol_count = _symbol_count(conn, directory)
    conn.close()

    print_results(directory, violations, symbol_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
