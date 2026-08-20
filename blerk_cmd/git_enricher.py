from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from blerk import config, coordinator, daemon_util, db
from blerk_cmd.util import normalize_dir


QUEUE = "git_queue"
TARGET_COL = "file_id"
DAEMON = "git-enricher"

log = logging.getLogger("git-enricher")


def find_git_root(directory: str) -> str | None:
    directory = normalize_dir(directory)
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def find_common_git_root(directory: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        common_dir = result.stdout.strip()
        if not os.path.isabs(common_dir):
            common_dir = os.path.normpath(os.path.join(directory, common_dir))
        return normalize_dir(os.path.dirname(common_dir))
    except Exception:
        return None


def get_or_create_repository(conn: sqlite3.Connection, common_root: str) -> int:
    row = conn.execute("SELECT id FROM repository WHERE path=?", (common_root,)).fetchone()
    if row:
        return int(row[0])

    url = ""
    try:
        r = subprocess.run(
            ["git", "-C", common_root, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            url = r.stdout.strip()
    except Exception:
        pass

    conn.execute("INSERT OR IGNORE INTO repository(path, url) VALUES(?, ?)", (common_root, url))
    row = conn.execute("SELECT id FROM repository WHERE path=?", (common_root,)).fetchone()
    return int(row[0])


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



def process_row(
    conn: sqlite3.Connection,
    cfg: config.Config,
    row: db.QueueRow,
    silent: bool = False,
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
    t0 = time.monotonic()

    root = find_git_root(os.path.dirname(path))
    if not root:
        try:
            db.mark_done(conn, QUEUE, row.id)
        except sqlite3.Error as e:
            log.warning("mark done git_queue %d: %s", row.id, e)
        return False, False

    common_root = find_common_git_root(os.path.dirname(path)) or root

    try:
        proc = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%H|%an|%D", "--", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
        repo_id = get_or_create_repository(conn, common_root)
        conn.execute(
            "INSERT INTO git_files(repository_id, file_id, git_branch, git_commit, git_author, git_enriched_at) "
            "VALUES(?, ?, ?, ?, ?, unixepoch()) "
            "ON CONFLICT(repository_id, file_id, git_branch) "
            "DO UPDATE SET git_commit=excluded.git_commit, git_author=excluded.git_author, "
            "git_enriched_at=excluded.git_enriched_at",
            (repo_id, row.target_id, branch, commit, author),
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

    if not silent:
        log.info("%s: %s, %s", DAEMON, daemon_util.fmt_duration(time.monotonic() - t0), path)
    return False, False


def run(cfg: config.Config, shutdown: threading.Event, silent: bool = False) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    client = coordinator.CoordinatorClient(QUEUE, cfg.db.path)
    poll = cfg.git_enricher.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = daemon_util.beginning_of_day(datetime.now())

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
                retried, failed = process_row(conn, cfg, row, silent=silent)
                if retried:
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                now = datetime.now()
                if (now - day_start).total_seconds() >= 24 * 3600:
                    day_start = daemon_util.beginning_of_day(now)
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
            db.write_heartbeat(conn, db.Heartbeat(
                DAEMON, status, queue_depth,
                processed_today, retries_today, failures_today,
                rate, eta, last_err,
            ))
        except sqlite3.Error as e:
            log.warning("heartbeat: %s", e)

        if client.wait(shutdown, poll):
            break

    client.close()
    try:
        conn.close()
    except sqlite3.Error:
        pass


def main() -> None:
    daemon_util.daemon_main(run)


if __name__ == "__main__":
    main()
