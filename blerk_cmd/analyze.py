from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

from blerk import config, db
from blerk_cmd.llm_describer import describe
from blerk_cmd.util import Scope, build_path_filters, normalize_dir, placeholders


@dataclass
class Finding:
    rule_id: int
    rule_name: str
    severity: str
    message: str
    confidence: float
    symbol_name: str
    file_path: str
    line: int


_build_path_filters = build_path_filters


def _fetch_symbols(
    conn,
    rule_ids: list[int],
    scope: Scope,
    kinds: list[str],
    min_lines: int,
    limit: int,
) -> list[tuple]:
    path_filters, path_params = _build_path_filters(scope)

    filters = [
        f"s.kind IN ({placeholders(len(kinds))})",
        "s.snippet IS NOT NULL",
        "s.snippet != ''",
        f"(COALESCE(s.end_line, s.line) - s.line) >= {int(min_lines)}",
    ]
    if rule_ids:
        filters.append(
            f"NOT EXISTS (SELECT 1 FROM findings fn"
            f" WHERE fn.symbol_id = s.id AND fn.rule_id IN ({placeholders(len(rule_ids))}))"
        )
    filters += path_filters

    where = " AND ".join(filters)
    limit_clause = f"LIMIT {int(limit)}" if limit > 0 else ""

    rows = conn.execute(
        f"""
        SELECT s.id, s.name, s.kind, f.path, s.line, s.snippet, s.description
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE {where}
        ORDER BY s.id
        {limit_clause}
        """,
        list(kinds) + list(rule_ids) + path_params,
    ).fetchall()

    return rows


def _fetch_refs(conn, symbol_id: int, max_callers: int, max_callees: int) -> tuple[list[str], list[str]]:
    callers = conn.execute(
        "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.caller_id"
        " WHERE r.callee_id=? LIMIT ?",
        (symbol_id, max_callers),
    ).fetchall()
    callees = conn.execute(
        "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.callee_id"
        " WHERE r.caller_id=? LIMIT ?",
        (symbol_id, max_callees),
    ).fetchall()
    return [r[0] for r in callers], [r[0] for r in callees]


def _build_prompt(
    name: str,
    kind: str,
    path: str,
    line: int,
    snippet: str,
    description: str | None,
    callers: list[str],
    callees: list[str],
    rules,
    max_callers: int,
    max_callees: int,
) -> str:
    caller_str = ", ".join(callers) if callers else "none"
    callee_str = ", ".join(callees) if callees else "none"
    desc_str = description or "none"

    rule_lines = "\n".join(
        f"{i + 1}. {r.name}: {r.description.strip()}"
        for i, r in enumerate(rules)
    )

    return (
        f"Symbol: {name} ({kind}) in {path}, line {line}\n"
        f"\n"
        f"--- snippet ---\n"
        f"{snippet}\n"
        f"--- end snippet ---\n"
        f"\n"
        f"Description: {desc_str}\n"
        f"\n"
        f"Callers (up to {max_callers}): {caller_str}\n"
        f"Callees (up to {max_callees}): {callee_str}\n"
        f"\n"
        f"Check this symbol against the following rules:\n"
        f"\n"
        f"{rule_lines}\n"
        f"\n"
        f'Return a JSON array. Each item must have these fields:\n'
        f'  "rule"       - the rule name from the list above\n'
        f'  "severity"   - "error", "warning", or "info"\n'
        f'  "message"    - one sentence explaining the finding\n'
        f'  "confidence" - a number from 0.0 to 1.0\n'
        f"\n"
        f"Return an empty array if no rules apply.\n"
        f"Return only the JSON array. No explanation. No markdown fences."
    )


def _parse_response(
    response: str,
    rule_name_to_id: dict[str, int],
    min_confidence: float,
) -> list[tuple]:
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    results = []
    for item in items:
        rule_name = item.get("rule", "")
        rule_id = rule_name_to_id.get(rule_name)
        if rule_id is None:
            continue
        confidence = float(item.get("confidence", 0.0))
        if confidence < min_confidence:
            continue
        message = str(item.get("message", ""))
        severity = str(item.get("severity", "info"))
        results.append((rule_id, rule_name, severity, message, confidence))
    return results


def _print_text(findings: list[Finding], checked: int) -> None:
    total = len(findings)
    print(f"FINDINGS  ({checked} symbols checked, {total} findings)")
    if not findings:
        return
    print()
    order = {"error": 0, "warning": 1, "info": 2}
    sorted_findings = sorted(
        findings,
        key=lambda f: (order.get(f.severity, 9), f.file_path, f.line),
    )
    for f in sorted_findings:
        loc = f"{f.file_path}:{f.line}"
        print(f"{f.severity:<8} {f.rule_name:<32} [{f.confidence:.2f}]  {loc}  {f.symbol_name}")
        print(f"         {f.message}")
        print()


def _print_json(findings: list[Finding]) -> None:
    out = [
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
    ]
    print(json.dumps(out, indent=2))


def _reset_findings(conn, rule_ids: list[int], scope: Scope) -> int:
    id_placeholders = ",".join("?" * len(rule_ids))
    path_filters, path_params = _build_path_filters(scope)
    if path_filters:
        where = " AND ".join(path_filters)
        result = conn.execute(
            f"""
            DELETE FROM findings
            WHERE rule_id IN ({id_placeholders})
              AND symbol_id IN (
                SELECT s.id FROM symbols s
                JOIN files f ON f.id = s.file_id
                WHERE {where}
              )
            """,
            rule_ids + path_params,
        )
    else:
        result = conn.execute(
            f"DELETE FROM findings WHERE rule_id IN ({id_placeholders})",
            rule_ids,
        )
    return result.rowcount


def run(
    conn,
    analyzer: config.Analyzer,
    rule_name_to_id: dict[str, int],
    scope: Scope,
    rule_filter: list[str],
    min_confidence: float,
    limit: int,
    no_save: bool,
    endpoint: str,
    model: str,
    api_key: str,
) -> tuple[list[Finding], int]:
    active_rules = [r for r in analyzer.rules if not rule_filter or r.name in rule_filter]
    if not active_rules:
        return [], 0

    active_rule_ids = [rule_name_to_id[r.name] for r in active_rules if r.name in rule_name_to_id]

    effective_scope = Scope(
        directory=scope.directory,
        exts=scope.exts or analyzer.extensions,
        excludes=scope.excludes,
    )
    symbols = _fetch_symbols(
        conn,
        active_rule_ids,
        effective_scope,
        analyzer.kinds,
        analyzer.min_lines,
        limit,
    )

    all_findings: list[Finding] = []
    total = len(symbols)

    for i, (sid, name, kind, path, line, snippet, description) in enumerate(symbols):
        print(f"  [{i + 1}/{total}] {name}  {os.path.basename(path)}", file=sys.stderr, flush=True)

        callers, callees = _fetch_refs(conn, sid, analyzer.max_context_callers, analyzer.max_context_callees)
        prompt = _build_prompt(
            name, kind, path, line, snippet or "", description,
            callers, callees, active_rules,
            analyzer.max_context_callers, analyzer.max_context_callees,
        )

        try:
            response = describe(endpoint, model, api_key, prompt)
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr, flush=True)
            continue

        parsed = _parse_response(response, rule_name_to_id, min_confidence)

        if not no_save and parsed:
            with db._write_lock:
                for rule_id, _, _, message, confidence in parsed:
                    conn.execute(
                        "INSERT OR REPLACE INTO findings(symbol_id, rule_id, message, confidence)"
                        " VALUES(?, ?, ?, ?)",
                        (sid, rule_id, message, confidence),
                    )

        for rule_id, rule_name, severity, message, confidence in parsed:
            all_findings.append(Finding(
                rule_id=rule_id,
                rule_name=rule_name,
                severity=severity,
                message=message,
                confidence=confidence,
                symbol_name=name,
                file_path=path,
                line=line,
            ))

    return all_findings, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run LLM-based analyzers against indexed symbols.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--analyzer", action="append", dest="analyzers", default=[], metavar="NAME",
                        help="run only this analyzer (repeatable; default: all)")
    parser.add_argument("directory", help="restrict to this directory")
    parser.add_argument("--ext", action="append", dest="exts", default=[], metavar="EXT")
    parser.add_argument("--exclude", action="append", dest="excludes", default=[], metavar="PATTERN",
                        help="exclude paths matching glob pattern (repeatable)")
    parser.add_argument("--rule", action="append", dest="rules", default=[], metavar="RULE",
                        help="run only this rule (repeatable)")
    parser.add_argument("--min-confidence", type=float, default=None, metavar="FLOAT")
    parser.add_argument("--limit", type=int, default=0, metavar="N")
    parser.add_argument("--output", choices=["text", "json"], default="text")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--reset", action="store_true",
                        help="delete existing findings for selected analyzers, then exit")
    args = parser.parse_args(argv)
    args.directory = normalize_dir(args.directory)
    scope = Scope(directory=args.directory, exts=args.exts, excludes=args.excludes)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)

    try:
        analyzers = config.load_analyzers_file(cfg.analyzers_file)
        if not analyzers:
            print("No analyzers configured.")
            print(f"Add [[analyzers]] blocks to: {cfg.analyzers_file}")
            return 1

        if args.analyzers:
            analyzers = [a for a in analyzers if a.name in args.analyzers]
            if not analyzers:
                print(f"No matching analyzers: {', '.join(args.analyzers)}")
                return 1

        rule_ids_by_analyzer = db.ensure_analyzers(conn, analyzers)

        if args.reset:
            total_deleted = 0
            for analyzer in analyzers:
                rule_ids = list(rule_ids_by_analyzer.get(analyzer.name, {}).values())
                if rule_ids:
                    total_deleted += _reset_findings(conn, rule_ids, scope)
            print(f"Deleted {total_deleted} findings.")
            return 0

        llm = cfg.llm[0] if cfg.llm else config.defaults().llm[0]
        all_findings: list[Finding] = []
        total_checked = 0

        for analyzer in analyzers:
            rule_name_to_id = rule_ids_by_analyzer.get(analyzer.name, {})
            endpoint = analyzer.endpoint or llm.endpoint
            model = analyzer.model or llm.model
            api_key = analyzer.api_key or llm.api_key
            min_confidence = (
                args.min_confidence if args.min_confidence is not None else analyzer.confidence
            )

            if not endpoint or not model:
                print(
                    f"Skipping '{analyzer.name}': no endpoint or model configured.",
                    file=sys.stderr,
                )
                continue

            print(f"Analyzer: {analyzer.name}", file=sys.stderr)
            findings, checked = run(
                conn, analyzer, rule_name_to_id, scope,
                args.rules, min_confidence, args.limit,
                args.no_save, endpoint, model, api_key,
            )
            total_checked += checked
            all_findings.extend(findings)

        if args.output == "json":
            _print_json(all_findings)
        else:
            _print_text(all_findings, total_checked)

    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
