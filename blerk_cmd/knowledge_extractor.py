from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

from blerk import config, daemon_util, db

DAEMON = "knowledge-extractor"

log = logging.getLogger(DAEMON)


def _transcripts_dir() -> Path:
    return Path.home() / ".blerk" / "transcripts"


def _claim() -> Path | None:
    td = _transcripts_dir()
    if not td.exists():
        return None
    candidates = sorted(td.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[0] if candidates else None


def _mark_done(path: Path) -> None:
    done_dir = path.parent / "done"
    done_dir.mkdir(exist_ok=True)
    dest = done_dir / path.name
    if dest.exists():
        dest.unlink()
    path.rename(dest)


def run(cfg: config.Config, shutdown: threading.Event) -> None:
    conn = db.open_db(cfg.db.path)
    llm = cfg.knowledge.llm
    poll = llm.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    status = "idle"
    last_err = ""

    while not shutdown.is_set():
        td = _transcripts_dir()
        queue_depth = len(list(td.glob("*.jsonl"))) if td.exists() else 0

        path = _claim()

        if path is None:
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

        status = "running"
        t0 = time.monotonic()

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            _process(conn, cfg, content, path.stem)
            _mark_done(path)
            processed_today += 1
            last_err = ""
            log.info("%s: processed %s in %.1fs", DAEMON, path.name, time.monotonic() - t0)

        except Exception as e:
            last_err = str(e)
            retries_today += 1
            log.warning("knowledge extract failed for %s: %s", path.name, e)

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


def _process(conn: sqlite3.Connection, cfg: config.Config, content: str, source_name: str) -> None:
    from blerk_cmd import extract_knowledge
    llm = cfg.knowledge.llm

    filtered = extract_knowledge.filter_transcript(content, llm.max_context_chars)
    if not filtered:
        log.info("%s: empty after filter, skipping", source_name)
        return

    log.info("%s: filtered to %d chars", source_name, len(filtered))
    prompt = llm.prompt_template.replace("{transcript}", filtered)
    try:
        response = extract_knowledge.call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
        items = extract_knowledge.parse_knowledge(response)
    except Exception as e:
        log.warning("%s: llm error: %s", source_name, e)
        return

    for item in items:
        cur = conn.execute(
            "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
            (item["concept"], item["pattern"], item["body"], "auto"),
        )
        conn.execute(
            "INSERT INTO knowledge_embed_queue(knowledge_id) VALUES (?)", (cur.lastrowid,)
        )
    conn.commit()
    log.info("%s: %d items extracted and queued for embedding", source_name, len(items))


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
