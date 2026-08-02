from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

from blerk import config, db
from blerk_cmd.llm_describer import describe


@dataclass
class Scope:
    directory: str = ""
    exts: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


def _build_path_filters(scope: Scope) -> tuple[list[str], list]:
    filters: list[str] = []
    params: list = []

    if scope.directory:
        fwd = scope.directory.replace("\\", "/").rstrip("/")
        bwd = scope.directory.replace("/", "\\").rstrip("\\")
        filters.append("(f.path LIKE ? OR f.path LIKE ? OR f.path LIKE ? OR f.path LIKE ?)")
        params += [f"%{fwd}/%", f"%{fwd}", f"%{bwd}\\%", f"%{bwd}"]

    if scope.exts:
        ext_conds = " OR ".join("f.path LIKE ?" for _ in scope.exts)
        filters.append(f"({ext_conds})")
        for ext in scope.exts:
            params.append(f"%{ext}")

    for pat in scope.excludes:
        sql = pat.replace("\\", "/").replace("*", "%").replace("?", "_")
        filters.append("f.path NOT LIKE ?")
        params.append(sql)

    return filters, params


def _fetch_symbols(conn, n: int, scope: Scope) -> list[tuple]:
    path_filters, params = _build_path_filters(scope)
    filters = [
        "s.kind IN ('function', 'method')",
        "s.snippet IS NOT NULL",
        "s.snippet != ''",
        "NOT EXISTS (SELECT 1 FROM symbol_tags st WHERE st.symbol_id = s.id AND st.key = 'confusing')",
    ] + path_filters

    where = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT s.id, s.name, s.kind, f.path, COALESCE(s.params, ''), s.snippet
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN symbol_refs sr ON sr.callee_id = s.id
        WHERE {where}
        GROUP BY s.id
        ORDER BY COUNT(sr.caller_id) DESC, (COALESCE(s.end_line, s.line) - s.line) DESC
        LIMIT ?
        """,
        params + [n],
    ).fetchall()

    return [(sid, name, kind, path, params_str, snippet)
            for sid, name, kind, path, params_str, snippet in rows]


def _count_already_tagged(conn, scope: Scope) -> int:
    path_filters, params = _build_path_filters(scope)
    filters = [
        "s.kind IN ('function', 'method')",
        "EXISTS (SELECT 1 FROM symbol_tags st WHERE st.symbol_id = s.id AND st.key = 'confusing')",
    ] + path_filters

    where = " AND ".join(filters)
    row = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id WHERE {where}",
        params,
    ).fetchone()
    return row[0] if row else 0


def _parse_response(text: str) -> tuple[bool | None, str]:
    stripped = text.strip()
    if stripped.startswith("CONFUSING:"):
        reason = stripped[len("CONFUSING:"):].strip()
        return True, reason
    if stripped.startswith("CLEAR"):
        return False, ""
    return None, ""


def reset_tags(conn, scope: Scope) -> int:
    path_filters, params = _build_path_filters(scope)
    filters = ["s.kind IN ('function', 'method')"] + path_filters
    where = " AND ".join(filters)
    result = conn.execute(
        f"""
        DELETE FROM symbol_tags
        WHERE key IN ('confusing', 'confusing_reason')
          AND symbol_id IN (
            SELECT s.id FROM symbols s
            JOIN files f ON f.id = s.file_id
            WHERE {where}
          )
        """,
        params,
    )
    return result.rowcount


def sweep(conn, cfg: config.Config, n: int, scope: Scope) -> None:
    c = cfg.antislop
    if not c.endpoint or not c.model:
        raise RuntimeError(
            "No [antislop] config found. Add endpoint and model to ~/.blerk/config.toml."
        )
    prompt_template = c.prompt
    already_tagged = _count_already_tagged(conn, scope)
    candidates = _fetch_symbols(conn, n, scope)

    assessed = 0
    confusing: list[tuple[str, str, str]] = []
    total = len(candidates)

    for i, (sid, name, kind, path, params_str, snippet) in enumerate(candidates):
        print(f"[{i+1}/{total}] {name}  {path.split('/')[-1]}", flush=True)
        prompt = (
            prompt_template
            .replace("{kind}", kind)
            .replace("{name}", name)
            .replace("{params}", params_str)
            .replace("{path}", path)
            .replace("{snippet}", snippet)
        )
        try:
            response = describe(c.endpoint, c.model, c.api_key, prompt)
        except Exception as e:
            print(f"  error: {e}", flush=True)
            continue

        is_confusing, reason = _parse_response(response)
        if is_confusing is None:
            print(f"  malformed response: {response!r}", flush=True)
            continue

        assessed += 1
        with db._write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO symbol_tags(symbol_id, key, value) VALUES (?, 'confusing', ?)",
                (sid, "true" if is_confusing else "false"),
            )
            if is_confusing:
                conn.execute(
                    "INSERT OR REPLACE INTO symbol_tags(symbol_id, key, value) VALUES (?, 'confusing_reason', ?)",
                    (sid, reason),
                )

        if is_confusing:
            confusing.append((name, path, reason))

    print(f"Assessed {assessed} symbols ({already_tagged} already tagged, skipped).")
    if confusing:
        print(f"{len(confusing)} confusing fragment{'s' if len(confusing) != 1 else ''}:\n")
        for name, path, reason in confusing:
            print(f"{name}  {path}")
            print(f"  {reason}")
            print()
    else:
        print("No confusing fragments found.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find confusing or pointless code fragments.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dir", default="", dest="directory", metavar="DIR")
    parser.add_argument("--ext", action="append", dest="exts", default=[], metavar="EXT")
    parser.add_argument("--exclude", action="append", dest="excludes", default=[], metavar="PATTERN",
                        help="exclude paths matching glob pattern (repeatable)")
    parser.add_argument("-n", type=int, default=50, metavar="N")
    parser.add_argument("--reset", action="store_true", help="clear existing confusing tags before sweeping")
    args = parser.parse_args(argv)
    args.directory = os.path.abspath(args.directory) if args.directory else os.getcwd()

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    try:
        if args.reset:
            reset_tags(conn, Scope(directory=args.directory))
            print("All antislop tags removed.")
            return 0
        sweep(conn, cfg, args.n, Scope(directory=args.directory, exts=args.exts, excludes=args.excludes))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
