from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

from blerk import config, db


QUEUE = "git_queue"
TARGET_COL = "file_id"
DAEMON = "git-enricher"

log = logging.getLogger("git-enricher")


def find_git_root(directory: str) -> str | None:
    directory = os.path.abspath(directory)
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def parse_branch(refs: str) -> str:
    for part in refs.split(","):
        part = part.strip()
        if part.startswith("HEAD -> "):
            return part[len("HEAD -> "):]
    for part in refs.split(","):
        part = part.strip()
        if part and part != "HEAD" and not part.startswith("origin/"):
            return part
    return ""


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


def process_row(
    conn: sqlite3.Connection,
    cfg: config.Config,
    row: db.QueueRow,
) -> tuple[bool, bool]:
    path_row = conn.execute(
        "SELECT path FROM files WHERE id=?", (row.target_id,)
    ).fetchone()
    if not path_row:
        try:
            db.mark_done(conn, QUEUE, row.id)
        except sqlite3.Error as e:
            log.warning("mark done git_queue %d: %s", row.id, e)
        return False, False
    path = path_row[0]

    root = find_git_root(os.path.dirname(path))
    if not root:
        try:
            db.mark_done(conn, QUEUE, row.id)
        except sqlite3.Error as e:
            log.warning("mark done git_queue %d: %s", row.id, e)
        return False, False

    try:
        proc = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%H|%an|%D", "--", path],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        try:
            failed = db.requeue(conn, QUEUE, row.id, str(e), cfg.git_enricher.max_retries)
        except sqlite3.Error as req_err:
            log.warning("requeue git_queue %d: %s", row.id, req_err)
            failed = False
        return True, failed

    line = proc.stdout.strip()
    if not line:
        try:
            db.mark_done(conn, QUEUE, row.id)
        except sqlite3.Error as e:
            log.warning("mark done git_queue %d: %s", row.id, e)
        return False, False

    parts = line.split("|", 2)
    commit = parts[0] if len(parts) > 0 else ""
    author = parts[1] if len(parts) > 1 else ""
    refs = parts[2] if len(parts) > 2 else ""
    branch = parse_branch(refs)

    try:
        conn.execute(
            "UPDATE files SET git_commit=?, git_author=?, git_branch=?, "
            "git_enriched_at=unixepoch() WHERE id=?",
            (commit, author, branch, row.target_id),
        )
    except sqlite3.Error as e:
        try:
            failed = db.requeue(conn, QUEUE, row.id, str(e), cfg.git_enricher.max_retries)
        except sqlite3.Error as req_err:
            log.warning("requeue git_queue %d: %s", row.id, req_err)
            failed = False
        return True, failed

    try:
        db.mark_done(conn, QUEUE, row.id)
    except sqlite3.Error as e:
        log.warning("mark done git_queue %d: %s", row.id, e)

    return False, False


def run(cfg: config.Config, shutdown: threading.Event) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    poll = cfg.git_enricher.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = beginning_of_day(datetime.now())

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        try:
            rows = db.claim_batch(conn, QUEUE, TARGET_COL, cfg.git_enricher.batch_size)
        except sqlite3.Error as e:
            log.warning("claim batch: %s", e)
            status = "error"
            last_err = str(e)
            rows = []

        if rows:
            status = "running"
            for row in rows:
                retried, failed = process_row(conn, cfg, row)
                if retried:
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                now = datetime.now()
                if (now - day_start).total_seconds() >= 24 * 3600:
                    day_start = beginning_of_day(now)
                    processed_today = 0
                    retries_today = 0
                    failures_today = 0
                rate_window.append(time.monotonic())
                processed_today += 1

        cutoff = time.monotonic() - 60.0
        while rate_window and rate_window[0] < cutoff:
            rate_window.pop(0)

        try:
            queue_depth = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {QUEUE} WHERE status='pending'"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            queue_depth = 0

        rate = float(len(rate_window))
        eta: int | None = None
        if rate > 0:
            eta = int(queue_depth / rate * 60)

        try:
            db.write_heartbeat(
                conn,
                DAEMON,
                status,
                queue_depth,
                processed_today,
                retries_today,
                failures_today,
                rate,
                eta,
                last_err,
            )
        except sqlite3.Error as e:
            log.warning("heartbeat: %s", e)

        if shutdown.wait(timeout=poll):
            break

    try:
        conn.close()
    except sqlite3.Error:
        pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args()

    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        log.error("load config: %s", e)
        sys.exit(1)

    shutdown = threading.Event()

    def _sig(_signum, _frame):
        shutdown.set()

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    run(cfg, shutdown)


if __name__ == "__main__":
    main()
