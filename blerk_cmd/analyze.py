from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

from blerk import config, db
from blerk_cmd.llm_describer import describe
from blerk_cmd.util import Scope, build_path_filters, placeholders


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
    stale: bool = False


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
        "EXISTS (SELECT 1 FROM code_blocks cb WHERE cb.symbol_id = s.id)",
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
        SELECT s.id, s.name, s.kind, f.path, s.line,
               COALESCE((SELECT content FROM code_blocks WHERE symbol_id=s.id AND block_index=0), ''),
               s.description
        FROM symbols s
        JOIN file_paths f ON f.file_id = s.file_id
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


def _print_text(findings: list[Finding], checked: int, unit: str = "symbols") -> None:
    total = len(findings)
    print(f"FINDINGS  ({checked} {unit} checked, {total} findings)")
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
                JOIN file_paths f ON f.file_id = s.file_id
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


_FILE_MODE_GUIDELINES = """\
You are a code reviewer. Review the following source file and report issues.

## Review Guidelines

### Tone
- Be humble.
- Provide actionable feedback, not vague criticism.
- Phrase findings as questions when uncertain, e.g. "Have you considered...?"

### Response Style
- Be concise but thorough.
- Include a brief code suggestion in your message when helpful.
- Only report genuine issues. Return an empty array if the code looks correct.

### Severity
- error: likely bug, security issue, or correctness problem
- warning: maintainability issue, code smell, or unclear design
- info: minor suggestion or improvement

### Confidence
Assign lower confidence when the issue is context-dependent or might be intentional.\
"""

_FILE_MODE_OUTPUT_FORMAT = """\
Return a JSON array. Each element must have:
  "rule"       - rule name from the list above
  "symbol"     - name of the symbol being flagged (or "" for file-level issues)
  "line"       - line number
  "severity"   - "error", "warning", or "info"
  "message"    - one sentence; phrase as a question if uncertain
  "confidence" - 0.0 to 1.0

Return [] if nothing is worth flagging.
Return only JSON. No markdown fences.\
"""

_MAX_FILE_SNIPPET_CHARS = 600
_MAX_FILE_PROMPT_CHARS = 12_000


def _fetch_files_in_scope(conn, scope: Scope, exts: list[str]) -> list[tuple]:
    effective = Scope(directory=scope.directory, exts=scope.exts or exts, excludes=scope.excludes)
    path_filters, path_params = _build_path_filters(effective)
    filters = ["EXISTS (SELECT 1 FROM code_blocks cb WHERE cb.symbol_id = s.id)"] + path_filters
    where = " AND ".join(filters)
    return conn.execute(
        f"""
        SELECT DISTINCT f.file_id, f.path
        FROM file_paths f
        JOIN symbols s ON s.file_id = f.file_id
        WHERE {where}
        ORDER BY f.path
        """,
        path_params,
    ).fetchall()


def _fetch_file_symbols(conn, file_id: int, max_callers: int, max_callees: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.name, s.kind, s.line, s.end_line,"
        " COALESCE((SELECT content FROM code_blocks WHERE symbol_id=s.id AND block_index=0), '') "
        "FROM symbols s WHERE s.file_id=?"
        " AND EXISTS (SELECT 1 FROM code_blocks cb WHERE cb.symbol_id = s.id)"
        " ORDER BY s.line",
        (file_id,),
    ).fetchall()
    result = []
    for sid, name, kind, line, end_line, snippet in rows:
        callers = [r[0] for r in conn.execute(
            "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.caller_id"
            " WHERE r.callee_id=? LIMIT ?", (sid, max_callers),
        ).fetchall()]
        callees = [r[0] for r in conn.execute(
            "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.callee_id"
            " WHERE r.caller_id=? LIMIT ?", (sid, max_callees),
        ).fetchall()]
        result.append({
            "id": sid, "name": name, "kind": kind,
            "line": line, "end_line": end_line or line,
            "snippet": snippet,
            "callers": callers, "callees": callees,
        })
    return result


def _build_file_prompt(path: str, symbols: list[dict], rules) -> str:
    rule_lines = "\n".join(
        f"{i + 1}. {r.name}: {r.description.strip()}"
        for i, r in enumerate(rules)
    )
    sym_parts = []
    for sym in symbols:
        caller_str = ", ".join(sym["callers"]) if sym["callers"] else "none"
        callee_str = ", ".join(sym["callees"]) if sym["callees"] else "none"
        snippet = sym["snippet"][:_MAX_FILE_SNIPPET_CHARS]
        sym_parts.append(
            f"### {sym['kind']} `{sym['name']}` (lines {sym['line']}–{sym['end_line']})\n"
            f"Called by: {caller_str}  |  Calls: {callee_str}\n\n"
            f"{snippet}"
        )

    sym_text = "\n\n".join(sym_parts)
    overhead = len(_FILE_MODE_GUIDELINES) + len(rule_lines) + len(_FILE_MODE_OUTPUT_FORMAT) + 100
    available = _MAX_FILE_PROMPT_CHARS - overhead
    if len(sym_text) > available:
        sym_text = sym_text[:available] + "\n... (truncated)"

    return (
        f"{_FILE_MODE_GUIDELINES}\n\n"
        f"## File: {path}\n\n"
        f"## Symbols\n\n{sym_text}\n\n"
        f"## Rules\n\n{rule_lines}\n\n"
        f"{_FILE_MODE_OUTPUT_FORMAT}"
    )


def _parse_file_response(
    response: str,
    rule_name_to_id: dict[str, int],
    min_confidence: float,
) -> list[dict]:
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
        if not isinstance(item, dict):
            continue
        rule_name = item.get("rule", "")
        rule_id = rule_name_to_id.get(rule_name)
        if rule_id is None:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
            line = int(item.get("line", 0))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        results.append({
            "rule_id": rule_id,
            "rule_name": rule_name,
            "symbol": str(item.get("symbol", "")),
            "line": line,
            "severity": str(item.get("severity", "info")),
            "message": str(item.get("message", "")),
            "confidence": confidence,
        })
    return results


def run_file_mode(
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

    files = _fetch_files_in_scope(conn, scope, analyzer.extensions)
    if limit > 0:
        files = files[:limit]

    all_findings: list[Finding] = []

    for i, (file_id, path) in enumerate(files):
        print(f"  [{i + 1}/{len(files)}] {os.path.basename(path)}", file=sys.stderr, flush=True)

        symbols = _fetch_file_symbols(conn, file_id, analyzer.max_context_callers, analyzer.max_context_callees)
        if not symbols:
            continue

        prompt = _build_file_prompt(path, symbols, active_rules)
        try:
            response = describe(endpoint, model, api_key, prompt)
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr, flush=True)
            continue

        parsed = _parse_file_response(response, rule_name_to_id, min_confidence)

        if not no_save and parsed:
            name_to_id = {s["name"]: s["id"] for s in symbols}
            with db._write_lock:
                for item in parsed:
                    sym_id = name_to_id.get(item["symbol"])
                    if sym_id is None:
                        log.warning("analyze: unrecognised symbol %r from LLM for %s", item["symbol"], path)
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO findings(symbol_id, rule_id, message, confidence, stale)"
                        " VALUES(?, ?, ?, ?, 0)",
                        (sym_id, item["rule_id"], item["message"], item["confidence"]),
                    )

        for item in parsed:
            sym_name = item["symbol"] or os.path.basename(path)
            line = item["line"] or symbols[0]["line"]
            all_findings.append(Finding(
                rule_id=item["rule_id"],
                rule_name=item["rule_name"],
                severity=item["severity"],
                message=item["message"],
                confidence=item["confidence"],
                symbol_name=sym_name,
                file_path=path,
                line=line,
            ))

    return all_findings, len(files)


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
        modes: set[str] = set()

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
            if analyzer.file_mode:
                findings, checked = run_file_mode(
                    conn, analyzer, rule_name_to_id, scope,
                    args.rules, min_confidence, args.limit,
                    args.no_save, endpoint, model, api_key,
                )
                modes.add("files")
            else:
                findings, checked = run(
                    conn, analyzer, rule_name_to_id, scope,
                    args.rules, min_confidence, args.limit,
                    args.no_save, endpoint, model, api_key,
                )
                modes.add("symbols")
            total_checked += checked
            all_findings.extend(findings)

        unit = "items" if len(modes) > 1 else (modes.pop() if modes else "symbols")
        if args.output == "json":
            _print_json(all_findings)
        else:
            _print_text(all_findings, total_checked, unit)

    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
