from __future__ import annotations

import argparse
import sys

from blerk import config, db
from blerk_cmd.util import normalize_dir


def detail(conn, name: str, path_filter: str = "") -> str:
    params: list = [name]
    path_sql = ""
    if path_filter:
        norm = normalize_dir(path_filter)
        path_sql = "AND f.path LIKE ?"
        params.append(f"%{norm}%")

    rows = conn.execute(
        f"""
        SELECT s.id, s.name, s.kind, f.path,
               s.line, COALESCE(s.end_line, s.line),
               COALESCE(s.description, ''),
               COALESCE(s.params, ''),
               s.nesting_depth, s.param_count
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.name = ? {path_sql}
        ORDER BY f.path, s.line
        """,
        params,
    ).fetchall()

    if not rows:
        return f"No symbol named '{name}' found."

    lines: list[str] = []
    if len(rows) > 1:
        lines.append(f"({len(rows)} symbols named '{name}', showing all)\n")
    for id_, sym_name, kind, path, line, end_line, desc, sig_params, nesting_depth, param_count in rows:
        sig = f"({sig_params})" if sig_params else ""
        lines.append(f"{kind} {sym_name}{sig}")
        lines.append(f"file: {path}  lines: {line}-{end_line}")
        attrs: list[str] = []
        if nesting_depth:
            attrs.append(f"depth {nesting_depth}")
        if param_count:
            attrs.append(f"{param_count} params")
        if attrs:
            lines.append("attrs: " + ", ".join(attrs))
        callers = conn.execute(
            "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.caller_id"
            " WHERE r.callee_id=? LIMIT 10",
            (id_,),
        ).fetchall()
        callees = conn.execute(
            "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.callee_id"
            " WHERE r.caller_id=? LIMIT 10",
            (id_,),
        ).fetchall()
        if callers:
            lines.append("callers: " + ", ".join(r[0] for r in callers))
        if callees:
            lines.append("callees: " + ", ".join(r[0] for r in callees))

        blocks = conn.execute(
            "SELECT content FROM code_blocks WHERE symbol_id=? ORDER BY block_index",
            (id_,),
        ).fetchall()
        if blocks:
            lines.append("content:")
            for bi, (block_content,) in enumerate(blocks):
                if bi > 0:
                    lines.append("  ---")
                for bl in block_content.splitlines():
                    lines.append("  " + bl)

        lines.append("")

    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show full detail for a symbol by name.")
    parser.add_argument("name", help="exact symbol name")
    parser.add_argument("--file", default="", dest="path_filter",
                        metavar="PATH", help="restrict to a file path substring")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(detail(conn, args.name, args.path_filter))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
