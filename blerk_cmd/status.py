from __future__ import annotations

import argparse
from datetime import datetime, timezone

from blerk import config, db
from blerk.coordinator import _port_file, _workers_dir


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


def _record_queue_counts(conn) -> None:
    """Record current queue counts to history for ETC calculation."""
    now = int(datetime.now(timezone.utc).timestamp())
    queues = {
        "symbolizer": "symbol_queue",
        "git-enricher": "git_queue",
        "fingerprinter": "fingerprint_queue",
        "embedder": "code_block_embed_queue",
        "llm-describer": "describe_queue",
    }

    for name, table in queues.items():
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE status='pending'").fetchone()[0]
            conn.execute(
                "INSERT INTO queue_history(queue_name, queue_count, timestamp) VALUES (?, ?, ?)",
                (name, count, now)
            )
        except Exception:
            pass

    conn.commit()

    # Clean up old records (keep last 24 hours)
    cutoff = now - 86400
    conn.execute("DELETE FROM queue_history WHERE timestamp < ?", (cutoff,))
    conn.commit()


def _get_queue_eta(conn, queue_name: str) -> int | None:
    """Calculate ETC in seconds from queue history."""
    # Get last 2 measurements
    rows = conn.execute(
        "SELECT queue_count, timestamp FROM queue_history WHERE queue_name = ? "
        "ORDER BY timestamp DESC LIMIT 2",
        (queue_name,)
    ).fetchall()

    if len(rows) < 2:
        return None

    count_now, ts_now = rows[0]
    count_prev, ts_prev = rows[1]

    if count_now <= 0 or ts_now == ts_prev:
        return None

    time_delta = ts_now - ts_prev
    items_processed = max(0, count_prev - count_now)

    if items_processed <= 0:
        return None

    rate_per_sec = items_processed / time_delta
    eta_sec = int(count_now / rate_per_sec)

    return max(1, eta_sec)


def status(conn, db_path: str = "") -> str:
    # Record current queue state for ETC calculation
    _record_queue_counts(conn)

    raw_heartbeats: list[tuple] = conn.execute(
        "SELECT daemon, status, queue_depth, rate_per_minute, eta_seconds, last_heartbeat, last_error "
        "FROM daemon_status ORDER BY daemon"
    ).fetchall()

    def _aggregate(prefix: str) -> tuple | None:
        matches = [r for r in raw_heartbeats if r[0] == prefix or r[0].startswith(prefix + "-")]
        if not matches:
            return None
        best = max(matches, key=lambda r: r[5] or 0)
        queue = best[2] or 0
        err = next((r[6] for r in sorted(matches, key=lambda r: r[5] or 0, reverse=True) if r[6]), "")
        stat = "running" if any(r[1] == "running" for r in matches) else best[1]
        return (best[0], stat, queue, best[5], err)


    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_syms = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind != 'heading'"
    ).fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(DISTINCT cb.symbol_id) FROM embeddings e JOIN code_blocks cb ON cb.id = e.block_id"
    ).fetchone()[0]
    describable = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind IN ('function','method')"
    ).fetchone()[0]
    described = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind IN ('function','method') AND description IS NOT NULL"
    ).fetchone()[0]

    lines: list[str] = []

    if db_path:
        try:
            port = int(_port_file(db_path).read_text().strip())
            wd = _workers_dir(db_path)
            workers = len(list(wd.glob("*.worker"))) if wd.exists() else 0
            lines.append(f"{'coordinator':<20}  running (port {port}, {workers} workers)")
        except (OSError, ValueError):
            lines.append(f"{'coordinator':<20}  not running")

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

    for label, db_name, mode in [
        ("watch-folder",  "watch-folder",  "files"),
        ("symbolizer",    "symbolizer",    "queue"),
        ("git-enricher",  "git-enricher",  "queue"),
        ("fingerprinter", "fingerprinter", "queue"),
        ("describer",     "llm-describer", "pct_described"),
        ("embedder",      "embedder",      "pct_embedded"),
    ]:
        hb = _aggregate(db_name)
        eta = None
        ts = None
        err = ""
        queue = 0
        stat = "unknown"
        if hb:
            _, stat, queue, ts, err = hb

        if mode == "files":
            detail = f"{total_files} files"
        elif mode == "queue":
            detail = f"{queue} pending" if queue else "idle"
            if queue:
                eta = _get_queue_eta(conn, label)
        elif mode == "pct_described":
            detail = _pct(described, describable)
        else:
            detail = _pct(embedded, total_syms)

        lines.append(_row(label, detail, eta, ts, err))

    return "\n".join(lines) if lines else "No daemon status found."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show daemon status.")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(status(conn, cfg.db.path))
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
