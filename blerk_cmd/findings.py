from __future__ import annotations

import argparse
import json
import sys

from blerk import config, db
from blerk_cmd.analyze import Finding
from blerk_cmd.util import Scope, build_path_filters as _build_path_filters


def _fetch_findings(
    conn,
    scope: Scope,
    analyzers: list[str],
    rules: list[str],
    severity: str,
    min_confidence: float,
) -> list[Finding]:
    path_filters, path_params = _build_path_filters(scope)

    filters = ["fn.confidence > 0"]
    params: list = []

    if analyzers:
        placeholders = ",".join("?" * len(analyzers))
        filters.append(f"a.name IN ({placeholders})")
        params += analyzers

    if rules:
        placeholders = ",".join("?" * len(rules))
        filters.append(f"ar.name IN ({placeholders})")
        params += rules

    if severity:
        filters.append("ar.severity = ?")
        params.append(severity)

    if min_confidence > 0:
        filters.append("fn.confidence >= ?")
        params.append(min_confidence)

    filters += path_filters
    params += path_params

    where = " AND ".join(filters)

    rows = conn.execute(
        f"""
        SELECT ar.name, ar.severity, fn.message, fn.confidence,
               s.name, f.path, s.line, fn.rule_id, fn.stale
        FROM findings fn
        JOIN analyzer_rules ar ON ar.id = fn.rule_id
        JOIN analyzers a ON a.id = ar.analyzer_id
        JOIN symbols s ON s.id = fn.symbol_id
        JOIN files f ON f.id = s.file_id
        WHERE {where}
        ORDER BY ar.severity, f.path, s.line
        """,
        params,
    ).fetchall()

    return [
        Finding(
            rule_id=rule_id,
            rule_name=rule_name,
            severity=severity,
            message=message,
            confidence=confidence,
            symbol_name=symbol_name,
            file_path=path,
            line=line,
            stale=bool(stale),
        )
        for rule_name, severity, message, confidence, symbol_name, path, line, rule_id, stale in rows
    ]


def _print_text(findings: list[Finding]) -> None:
    if not findings:
        print("No findings.")
        return
    order = {"error": 0, "warning": 1, "info": 2}
    for f in sorted(findings, key=lambda x: (order.get(x.severity, 9), x.file_path, x.line)):
        loc = f"{f.file_path}:{f.line}"
        stale = " [STALE]" if f.stale else ""
        print(f"{f.severity:<8} {f.rule_name:<32} [{f.confidence:.2f}]{stale}  {loc}  {f.symbol_name}")
        if f.message:
            print(f"         {f.message}")
        print()


def _print_json(findings: list[Finding]) -> None:
    print(json.dumps([
        {
            "rule": f.rule_name,
            "severity": f.severity,
            "message": f.message,
            "confidence": f.confidence,
            "symbol_name": f.symbol_name,
            "file_path": f.file_path,
            "line": f.line,
        }
        for f in findings
    ], indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show stored analyzer findings.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("directory", help="restrict to this directory")
    parser.add_argument("--ext", action="append", dest="exts", default=[], metavar="EXT")
    parser.add_argument("--exclude", action="append", dest="excludes", default=[], metavar="PATTERN")
    parser.add_argument("--analyzer", action="append", dest="analyzers", default=[], metavar="NAME")
    parser.add_argument("--rule", action="append", dest="rules", default=[], metavar="RULE")
    parser.add_argument("--severity", choices=["error", "warning", "info"], default="")
    parser.add_argument("--min-confidence", type=float, default=0.0, metavar="FLOAT")
    parser.add_argument("--output", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    scope = Scope(directory=args.directory, exts=args.exts, excludes=args.excludes)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    try:
        findings = _fetch_findings(
            conn, scope, args.analyzers, args.rules, args.severity, args.min_confidence,
        )
        if args.output == "json":
            _print_json(findings)
        else:
            print(f"{len(findings)} finding{'s' if len(findings) != 1 else ''}.")
            print()
            _print_text(findings)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
