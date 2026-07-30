from __future__ import annotations

import argparse
from datetime import datetime, timezone

from blerk import config, db


def _fmt_eta(seconds: int | None) -> str:
    if not seconds:
        return ""
    if seconds < 60:
        return f"eta {seconds}s"
    if seconds < 3600:
        return f"eta {seconds // 60}m"
    return f"eta {seconds // 3600}h{(seconds % 3600) // 60}m"


def _fmt_age(ts: int) -> str:
    age = int(datetime.now(timezone.utc).timestamp()) - ts
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "100%"
    return f"{num * 100 // den}%"


def status(conn) -> str:
    raw_heartbeats: list[tuple] = conn.execute(
        "SELECT daemon, status, queue_depth, rate_per_minute, eta_seconds, last_heartbeat, last_error "
        "FROM daemon_status ORDER BY daemon"
    ).fetchall()

    def _latest(prefix: str) -> tuple | None:
        matches = [r for r in raw_heartbeats if r[0] == prefix or r[0].startswith(prefix + "-")]
        if not matches:
            return None
        return max(matches, key=lambda r: r[5] or 0)


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

    lines: list[str] = []

    def _row(name: str, detail: str, eta: int | None, heartbeat: int | None, error: str) -> str:
        parts = [f"{name:<20}", detail]
        eta_str = _fmt_eta(eta)
        if eta_str:
            parts.append(eta_str)
        if heartbeat:
            parts.append(f"({_fmt_age(heartbeat)})")
        result = "  ".join(p for p in parts if p)
        if error:
            result += f"\n  error: {error}"
        return result

    for label, db_name in [
        ("watch-folder", "watch-folder"),
        ("symbolizer",   "symbolizer"),
        ("describer",    "llm-describer"),
        ("embedder",     "embedder"),
    ]:
        hb = _latest(db_name)
        eta = None
        ts = None
        err = ""
        queue = 0
        stat = "unknown"
        if hb:
            _, stat, queue, _, eta, ts, err = hb
            err = err or ""

        if label == "watch-folder":
            detail = f"{total_files} files"
        elif label == "symbolizer":
            detail = f"{queue} pending" if queue else "idle"
            if not queue:
                eta = None
        elif label == "describer":
            detail = _pct(described, describable)
            if stat == "idle":
                eta = None
        else:
            detail = _pct(embedded, total_syms)
            if stat == "idle":
                eta = None

        lines.append(_row(label, detail, eta, ts, err))

    return "\n".join(lines) if lines else "No daemon status found."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show daemon status.")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(status(conn))
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
