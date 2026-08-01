from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime

from blerk import config, coordinator, daemon_util, db

QUEUE = "fingerprint_queue"
TARGET_COL = "symbol_id"
DAEMON = "fingerprinter"
BATCH_SIZE = 200
POLL_S = 2.0

log = logging.getLogger(DAEMON)

_WHITESPACE = re.compile(r"\s+")


def _normalize(snippet: str) -> str:
    return _WHITESPACE.sub(" ", snippet.lower()).strip()


def normhash(snippet: str) -> str:
    return hashlib.sha256(_normalize(snippet).encode()).hexdigest()


def simhash(snippet: str, n: int = 4) -> str:
    text = _normalize(snippet)
    bits = [0] * 64
    for i in range(max(0, len(text) - n + 1)):
        gram = text[i : i + n].encode()
        h = int(hashlib.md5(gram).hexdigest(), 16)
        for bit in range(64):
            bits[bit] += 1 if h & (1 << bit) else -1
    value = 0
    for bit in range(64):
        if bits[bit] > 0:
            value |= 1 << bit
    return format(value, "016x")


def fingerprint(snippet: str) -> dict[str, str]:
    return {"normhash": normhash(snippet), "simhash": simhash(snippet)}


def run(cfg: config.Config, shutdown: threading.Event, silent: bool = False) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    client = coordinator.CoordinatorClient(QUEUE, cfg.db.path)
    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        try:
            rows = db.claim_batch(conn, QUEUE, TARGET_COL, BATCH_SIZE)
        except sqlite3.Error as e:
            log.warning("claim batch: %s", e)
            status = "error"
            last_err = str(e)
            rows = []

        if rows:
            status = "running"
            for row in rows:
                sym_row = conn.execute(
                    "SELECT name, snippet FROM symbols WHERE id=?",
                    (row.target_id,),
                ).fetchone()

                if not sym_row or not sym_row[1]:
                    try:
                        db.mark_done(conn, QUEUE, row.id)
                    except sqlite3.Error as e:
                        log.warning("mark done %d: %s", row.id, e)
                    continue

                sym_name, snippet = sym_row[0], sym_row[1]
                t0 = time.monotonic()
                try:
                    fps = fingerprint(snippet)
                    for kind, value in fps.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO fingerprints(symbol_id, kind, value) VALUES (?,?,?)",
                            (row.target_id, kind, value),
                        )
                    db.mark_done(conn, QUEUE, row.id)
                    if not silent:
                        log.info("%s: %s, %s", DAEMON, daemon_util.fmt_duration(time.monotonic() - t0), sym_name)
                except sqlite3.Error as e:
                    log.warning("write fingerprint %d: %s", row.target_id, e)
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), 3)
                    except sqlite3.Error as req_err:
                        log.warning("requeue %d: %s", row.id, req_err)
                        failed = False
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                now = datetime.now()
                if (now - day_start).total_seconds() >= 24 * 3600:
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
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
        eta: int | None = int(queue_depth / rate * 60) if rate > 0 else None

        try:
            db.write_heartbeat(conn, db.Heartbeat(
                DAEMON, status, queue_depth,
                processed_today, retries_today, failures_today,
                rate, eta, last_err,
            ))
        except sqlite3.Error as e:
            log.warning("heartbeat: %s", e)

        if client.wait(shutdown, POLL_S):
            break

    client.close()
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

    parser = argparse.ArgumentParser(description="Compute normhash and simhash fingerprints for indexed symbols.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()

    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        log.error("load config: %s", e)
        sys.exit(1)

    daemon_util.setup_logging(args.silent or cfg.silent)

    shutdown = daemon_util.make_shutdown()
    run(cfg, shutdown, silent=args.silent or cfg.silent)


if __name__ == "__main__":
    main()
