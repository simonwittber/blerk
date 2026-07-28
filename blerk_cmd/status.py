from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from blerk import config, db


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _fmt_age(ts: int) -> str:
    age = int(datetime.now(timezone.utc).timestamp()) - ts
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def _folder_stats(conn, folder: str) -> tuple[int, int, int, int, int]:
    norm = folder.replace("\\", "/").rstrip("/")
    pattern = f"{norm}/%"
    files = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ?", (pattern,)
    ).fetchone()[0]
    describable = conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        " WHERE f.path LIKE ? AND s.kind IN ('function','method')", (pattern,)
    ).fetchone()[0]
    described = conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        " WHERE f.path LIKE ? AND s.kind IN ('function','method') AND s.description IS NOT NULL",
        (pattern,),
    ).fetchone()[0]
    embeddable = conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id"
        " WHERE f.path LIKE ? AND s.kind != 'heading'", (pattern,)
    ).fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(DISTINCT e.symbol_id) FROM embeddings e"
        " JOIN symbols s ON s.id = e.symbol_id"
        " JOIN files f ON f.id = s.file_id"
        " WHERE f.path LIKE ? AND s.kind != 'heading'", (pattern,)
    ).fetchone()[0]
    return files, describable, described, embeddable, embedded


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "n/a"
    return f"{num * 100 // den}%"


def status(conn, folders: list[str]) -> str:
    rows = conn.execute(
        """
        SELECT daemon, status, queue_depth, processed_today, retries_today,
               failures_today, rate_per_minute, eta_seconds, last_heartbeat, last_error
        FROM daemon_status
        ORDER BY daemon
        """
    ).fetchall()

    lines: list[str] = []

    if rows:
        for daemon, stat, queue, processed, retries, failures, rate, eta, heartbeat, last_err in rows:
            age = _fmt_age(heartbeat)
            eta_str = _fmt_eta(eta)
            rate_str = f"{rate:.1f}/min" if rate else ""
            parts = [f"{daemon:<20} {stat:<8} queue={queue:<6} done={processed:<6}"]
            if rate_str:
                parts.append(f"rate={rate_str}")
            if eta_str:
                parts.append(f"eta={eta_str}")
            if retries:
                parts.append(f"retries={retries}")
            if failures:
                parts.append(f"failures={failures}")
            parts.append(f"({age})")
            lines.append(" ".join(parts))
            if last_err:
                lines.append(f"  error: {last_err}")
    else:
        lines.append("No daemon status found.")

    if folders:
        lines.append("")
        lines.append("Watched folders:")
        for folder in folders:
            files, describable, described, embeddable, embedded = _folder_stats(conn, folder)
            desc_pct = _pct(described, describable)
            embed_pct = _pct(embedded, embeddable)
            lines.append(f"  {folder}")
            lines.append(f"    files={files}  described={desc_pct}  embedded={embed_pct}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show daemon status.")
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    print(status(conn, cfg.watch.folders))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
