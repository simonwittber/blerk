from __future__ import annotations

import sys

from blerk import config, db
from blerk_cmd.util import normalize_dir


def summary(cfg: config.Config, directory: str = "") -> str:
    try:
        conn = db.open_db(cfg.db.path)
    except Exception:
        return ""

    dir_sql = ""
    dir_params: list = []
    if directory:
        norm = normalize_dir(directory).rstrip("/")
        dir_sql = "AND (path LIKE ? OR path LIKE ?)"
        dir_params = [f"%{norm}/%", f"%{norm}"]

    sym_dir_sql = ""
    sym_dir_params: list = []
    if directory:
        norm = normalize_dir(directory).rstrip("/")
        sym_dir_sql = "AND (f.path LIKE ? OR f.path LIKE ?)"
        sym_dir_params = [f"%{norm}/%", f"%{norm}"]

    total_files = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE 1=1 {dir_sql}", dir_params
    ).fetchone()[0]
    total_syms = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        f" WHERE s.kind != 'heading' {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    embedded = conn.execute(
        f"SELECT COUNT(DISTINCT e.symbol_id) FROM embeddings e"
        f" JOIN symbols s ON s.id = e.symbol_id JOIN files f ON f.id = s.file_id"
        f" WHERE 1=1 {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    describable = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        f" WHERE s.kind IN ('function','method') {sym_dir_sql}", sym_dir_params
    ).fetchone()[0]
    described = conn.execute(
        f"SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        f" WHERE s.kind IN ('function','method') AND s.description IS NOT NULL {sym_dir_sql}",
        sym_dir_params
    ).fetchone()[0]
    recent = conn.execute(
        f"SELECT path FROM files WHERE mtime > unixepoch() - 604800 {dir_sql}"
        f" ORDER BY mtime DESC LIMIT 10", dir_params
    ).fetchall()
    findings = conn.execute(
        f"SELECT r.severity, COUNT(*) FROM findings fi"
        f" JOIN analyzer_rules r ON r.id = fi.rule_id"
        f" JOIN symbols s ON s.id = fi.symbol_id JOIN files f ON f.id = s.file_id"
        f" WHERE 1=1 {sym_dir_sql}"
        f" GROUP BY r.severity ORDER BY r.severity", sym_dir_params
    ).fetchall()
    conn.close()

    emb_pct = int(embedded / total_syms * 100) if total_syms else 0
    desc_pct = int(described / describable * 100) if describable else 0

    lines: list[str] = [f"Blerk index: {directory or 'all'}", ""]
    lines.append(f"Files: {total_files:,} | Symbols: {total_syms:,} | Embeddings: {emb_pct}% | Descriptions: {desc_pct}%")

    if recent:
        lines.append("")
        lines.append("Recent changes (7 days):")
        for (path,) in recent:
            lines.append(f"  {path}")

    if findings:
        lines.append("")
        parts = [f"{count} {sev}" for sev, count in findings]
        lines.append("Findings: " + ", ".join(parts))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Print a project index snapshot.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("directory", help="restrict to this directory")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except Exception as e:
        print(f"blerk: {e}", file=sys.stderr)
        return 1

    text = summary(cfg, normalize_dir(args.directory))
    if not text:
        print("blerk: database not found.")
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
