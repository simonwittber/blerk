from __future__ import annotations

import argparse
from datetime import datetime, timezone

from typing import TYPE_CHECKING

from blerk import config, db
from blerk.coordinator import _port_file, _workers_dir

if TYPE_CHECKING:
    import sqlite3 as _sqlite3


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


def _get_queue_eta(conn, queue_name: str, queue_count: int, fallback_rate_per_min: float = 0.0) -> int | None:
    """Calculate ETC in seconds from queue history, or fallback to heartbeat rate."""
    if queue_count <= 0:
        return None

    # Get last 2 measurements
    rows = conn.execute(
        "SELECT queue_count, timestamp FROM queue_history WHERE queue_name = ? "
        "ORDER BY timestamp DESC LIMIT 2",
        (queue_name,)
    ).fetchall()

    if len(rows) >= 2:
        count_now, ts_now = rows[0]
        count_prev, ts_prev = rows[1]

        if ts_now != ts_prev:
            time_delta = ts_now - ts_prev
            items_processed = max(0, count_prev - count_now)

            if items_processed > 0:
                rate_per_sec = items_processed / time_delta
                eta_sec = int(count_now / rate_per_sec)
                return max(1, eta_sec)

    # Fallback to heartbeat rate if no history
    if fallback_rate_per_min > 0:
        rate_per_sec = fallback_rate_per_min / 60.0
        eta_sec = int(queue_count / rate_per_sec)
        return max(1, eta_sec)

    return None


def _hints_stats(conn: "_sqlite3.Connection") -> list[str]:
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM hints GROUP BY source"
    ).fetchall()
    counts = {src: n for src, n in rows}
    explicit = counts.get("explicit", 0)
    auto = counts.get("auto", 0)
    total = explicit + auto
    hint_line = f"{total} total ({explicit} explicit, {auto} auto)"

    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM hint_extract_queue WHERE status='pending'"
        ).fetchone()[0]
        queue_line = f"{pending} pending"
    except Exception:
        queue_line = "unavailable"

    return [
        f"{'hints':<20}  {hint_line}",
        f"{'hint-queue':<20}  {queue_line}",
    ]


def status(conn, db_path: str = "", cfg: "config.Config | None" = None) -> str:
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
        rate = best[3] or 0.0
        err = next((r[6] for r in sorted(matches, key=lambda r: r[5] or 0, reverse=True) if r[6]), "")
        stat = "running" if any(r[1] == "running" for r in matches) else best[1]
        return (best[0], stat, queue, rate, best[5], err)


    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

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

    daemon_list = [
        ("watch-folder",  "watch-folder"),
        ("symbolizer",    "symbolizer"),
        ("git-enricher",  "git-enricher"),
        ("fingerprinter", "fingerprinter"),
        ("describer",     "llm-describer"),
        ("embedder",      "embedder"),
    ]
    if cfg is not None and cfg.hints.llm.enabled:
        daemon_list.append(("hint-extractor", "hint-extractor"))

    for label, db_name in daemon_list:
        hb = _aggregate(db_name)
        eta = None
        ts = None
        err = ""
        queue = 0
        rate = 0.0
        stat = "unknown"
        if hb:
            _, stat, queue, rate, ts, err = hb

        if label == "watch-folder":
            detail = f"{total_files} files"
        else:
            detail = f"{queue} pending" if queue else "idle"
            if queue:
                eta = _get_queue_eta(conn, label, queue, rate)

        lines.append(_row(label, detail, eta, ts, err))

    lines.extend(_hints_stats(conn))

    return "\n".join(lines) if lines else "No daemon status found."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show daemon status.")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(status(conn, cfg.db.path, cfg))
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
