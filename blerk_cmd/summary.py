from __future__ import annotations

import sys

from blerk import config, db
from blerk_cmd.util import Scope, index_root, scope_clause, to_relative


def summary(cfg: config.Config, directory: str = "", root: str = "") -> str:
    try:
        conn = db.open_db(cfg.db.path)
    except Exception:
        return ""

    # The same scope is applied twice because file_paths is queried both directly and through a join alias.
    scope = Scope(directory=directory, root=root)
    dir_sql, dir_params = scope_clause(scope, "path")
    sym_dir_sql, sym_dir_params = scope_clause(scope, "f.path")

    total_files = conn.execute(
        f"SELECT COUNT(*) FROM file_paths WHERE 1=1 {dir_sql}", dir_params
    ).fetchone()[0]
    total_syms = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN file_paths f ON f.file_id = s.file_id"
        f" WHERE s.kind != 'heading' {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    embedded = conn.execute(
        f"SELECT COUNT(DISTINCT s.id) FROM embeddings e"
        f" JOIN code_blocks cb ON cb.content_hash = e.content_hash"
        f" JOIN symbols s ON s.id = cb.symbol_id JOIN file_paths f ON f.file_id = s.file_id"
        f" WHERE s.kind != 'heading' {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    describable = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN file_paths f ON f.file_id = s.file_id"
        f" WHERE s.kind IN ('function','method') {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    described = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN file_paths f ON f.file_id = s.file_id"
        f" WHERE s.kind IN ('function','method') AND s.description IS NOT NULL {sym_dir_sql}",
        sym_dir_params
    ).fetchone()[0]
    recent = conn.execute(
        f"SELECT path FROM file_paths WHERE mtime > unixepoch() - 604800 {dir_sql}"
        f" ORDER BY mtime DESC LIMIT 10", dir_params
    ).fetchall()
    findings = conn.execute(
        f"SELECT r.severity, COUNT(*) FROM findings fi"
        f" JOIN analyzer_rules r ON r.id = fi.rule_id"
        f" JOIN symbols s ON s.id = fi.symbol_id JOIN file_paths f ON f.file_id = s.file_id"
        f" WHERE 1=1 {sym_dir_sql}"
        f" GROUP BY r.severity ORDER BY r.severity", sym_dir_params
    ).fetchall()
    conn.close()

    emb_pct = int(embedded / total_syms * 100) if total_syms else 0
    desc_pct = int(described / describable * 100) if describable else 0

    lines: list[str] = [f"Blerk index: {directory or root or 'all'}", ""]
    lines.append(f"Files: {total_files:,} | Symbols: {total_syms:,} | Embeddings: {emb_pct}% | Descriptions: {desc_pct}%")

    if recent:
        lines.append("")
        lines.append("Recent changes (7 days):")
        for (path,) in recent:
            lines.append(f"  {to_relative(path, root)}")

    if findings:
        lines.append("")
        parts = [f"{count} {sev}" for sev, count in findings]
        lines.append("Findings: " + ", ".join(parts))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Print a project index snapshot.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("directory", nargs="?", default="",
                        help="restrict to this directory, relative to the index root or absolute (default: the whole root)")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except Exception as e:
        print(f"blerk: {e}", file=sys.stderr)
        return 1

    text = summary(cfg, args.directory, index_root(cfg))
    if not text:
        print("blerk: database not found.")
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
