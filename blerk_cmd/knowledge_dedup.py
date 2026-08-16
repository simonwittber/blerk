from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import time

from blerk import config, daemon_util, db

DAEMON = "knowledge-dedup"
QUEUE = "knowledge_dedup_queue"

log = logging.getLogger(DAEMON)


def _claim(conn: sqlite3.Connection) -> tuple[int, int] | None:
    with db._write_lock:
        row = conn.execute(
            f"SELECT id, knowledge_id FROM {QUEUE} WHERE status='pending'"
            " ORDER BY queued_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(f"UPDATE {QUEUE} SET status='processing' WHERE id=?", (row[0],))
        conn.commit()
    return row[0], row[1]


def run(cfg: config.Config, shutdown: threading.Event) -> None:
    from blerk_cmd.extract_knowledge import dedup_knowledge, _unpack

    conn = db.open_db(cfg.db.path)
    llm = cfg.knowledge.llm
    poll = llm.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    status = "idle"
    last_err = ""

    while not shutdown.is_set():
        queue_depth = conn.execute(
            f"SELECT COUNT(*) FROM {QUEUE} WHERE status='pending'"
        ).fetchone()[0]

        claimed = _claim(conn)

        if not claimed:
            status = "idle"
            try:
                db.write_heartbeat(conn, db.Heartbeat(
                    DAEMON, status, queue_depth,
                    processed_today, retries_today, failures_today,
                    0.0, None, last_err,
                ))
            except sqlite3.Error as e:
                log.warning("heartbeat: %s", e)
            shutdown.wait(timeout=poll)
            continue

        queue_id, knowledge_id = claimed
        status = "running"
        t0 = time.monotonic()

        try:
            row = conn.execute(
                "SELECT k.body, k.concept, k.pattern, ke.vector"
                " FROM knowledge k"
                " JOIN knowledge_embeddings ke ON ke.knowledge_id = k.id"
                " WHERE k.id = ? AND ke.model = ?",
                (knowledge_id, cfg.embedder.model),
            ).fetchone()

            if row:
                body, concept, pattern, blob = row
                vec = _unpack(blob)
                dedup_knowledge(conn, cfg, knowledge_id, vec, body, concept, pattern)

            db.mark_queue_done(conn, QUEUE, queue_id)
            conn.commit()
            processed_today += 1
            last_err = ""
            log.info("%s: deduped knowledge_id=%d in %.1fs", DAEMON, knowledge_id, time.monotonic() - t0)

        except Exception as e:
            last_err = str(e)
            retries_today += 1
            log.warning("dedup failed for knowledge_id=%d: %s", knowledge_id, e)
            try:
                db.requeue(conn, QUEUE, queue_id, str(e), llm.max_retries)
                failures_today += 1
            except sqlite3.Error as req_err:
                log.warning("requeue %d: %s", queue_id, req_err)

        try:
            db.write_heartbeat(conn, db.Heartbeat(
                DAEMON, status, queue_depth,
                processed_today, retries_today, failures_today,
                0.0, None, last_err,
            ))
        except sqlite3.Error as e:
            log.warning("heartbeat: %s", e)

    try:
        conn.close()
    except sqlite3.Error:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()

    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        logging.basicConfig()
        log.error("load config: %s", e)
        sys.exit(1)

    daemon_util.setup_logging(args.silent or cfg.silent)

    if not cfg.knowledge.llm.enabled:
        log.info("knowledge LLM disabled; exiting")
        return

    shutdown = daemon_util.make_shutdown()
    run(cfg, shutdown)


if __name__ == "__main__":
    main()
