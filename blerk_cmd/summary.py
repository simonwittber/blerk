from __future__ import annotations

import sys

from blerk import config, db


def summary(cfg: config.Config) -> str:
    try:
        conn = db.open_db(cfg.db.path)
    except Exception:
        return ""

    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_syms = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind != 'heading'"
    ).fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(DISTINCT symbol_id) FROM embeddings"
    ).fetchone()[0]
    describable = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind IN ('function','method')"
    ).fetchone()[0]
    described = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind IN ('function','method') AND description IS NOT NULL"
    ).fetchone()[0]
    recent = conn.execute(
        "SELECT path FROM files WHERE mtime > unixepoch() - 604800 ORDER BY mtime DESC LIMIT 10"
    ).fetchall()
    findings = conn.execute(
        "SELECT r.severity, COUNT(*) FROM findings f"
        " JOIN analyzer_rules r ON r.id = f.rule_id"
        " GROUP BY r.severity ORDER BY r.severity"
    ).fetchall()
    conn.close()

    emb_pct = int(embedded / total_syms * 100) if total_syms else 0
    desc_pct = int(described / describable * 100) if describable else 0

    lines: list[str] = ["Blerk index", ""]
    lines.append("Watch folders:")
    for f in cfg.watch.folders:
        lines.append(f"  {f}")
    lines.append("")
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
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except Exception as e:
        print(f"blerk: {e}", file=sys.stderr)
        return 1

    text = summary(cfg)
    if not text:
        print("blerk: database not found.")
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
