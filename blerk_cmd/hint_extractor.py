from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import threading
import time

import httpx

from blerk import config, daemon_util, db

QUEUE = "hint_extract_queue"
DAEMON = "hint-extractor"

log = logging.getLogger(DAEMON)

_client = httpx.Client(timeout=120.0)
_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_transcript(content: str, max_chars: int) -> str:
    lines: list[str] = []
    total = 0
    for raw in content.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        role = msg.get("role", "")
        body = msg.get("content", "")
        if not isinstance(body, str):
            continue
        line = f"{role}: {body}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def _call_llm(endpoint: str, model: str, api_key: str, prompt: str) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = _client.post(endpoint + "/v1/chat/completions", json=body, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"llm {r.status_code}: {r.text[:200]}")
    choices = r.json().get("choices", [])
    if not choices:
        raise RuntimeError("empty llm response")
    return choices[0]["message"]["content"]


def _parse_hints(text: str) -> list[dict]:
    m = _JSON_RE.search(text)
    if not m:
        return []
    try:
        items = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept", "")).strip()
        pattern = str(item.get("pattern", "**")).strip()
        body = str(item.get("body", "")).strip()
        if concept and body:
            result.append({"concept": concept, "pattern": pattern or "**", "body": body})
    return result


def _claim(conn: sqlite3.Connection) -> tuple[int, str] | None:
    with db._write_lock:
        row = conn.execute(
            f"SELECT q.id, t.content FROM {QUEUE} q"
            " JOIN transcripts t ON t.id = q.transcript_id"
            " WHERE q.status='pending'"
            " ORDER BY q.priority DESC, q.queued_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(f"UPDATE {QUEUE} SET status='processing' WHERE id=?", (row[0],))
    return row[0], row[1]


def run(cfg: config.Config, shutdown: threading.Event) -> None:
    conn = db.open_db(cfg.db.path)
    llm = cfg.hints.llm
    poll = llm.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    status = "idle"
    last_err = ""

    while not shutdown.is_set():
        claimed = _claim(conn)

        queue_depth = conn.execute(
            f"SELECT COUNT(*) FROM {QUEUE} WHERE status='pending'"
        ).fetchone()[0]

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

        queue_id, raw_content = claimed
        status = "running"
        t0 = time.monotonic()

        try:
            transcript = _parse_transcript(raw_content, llm.max_context_chars)
            if not transcript:
                db.mark_queue_done(conn, QUEUE, queue_id)
                conn.commit()
                continue

            prompt = llm.prompt_template.replace("{transcript}", transcript)
            response = _call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
            hints = _parse_hints(response)

            for h in hints:
                try:
                    conn.execute(
                        "INSERT INTO hints(concept, pattern, body, source, queue_id) VALUES (?,?,?,?,?)",
                        (h["concept"], h["pattern"], h["body"], "auto", queue_id),
                    )
                except sqlite3.IntegrityError:
                    pass
            db.mark_queue_done(conn, QUEUE, queue_id)
            conn.commit()

            processed_today += 1
            last_err = ""
            log.info("%s: extracted %d hints in %.1fs", DAEMON, len(hints), time.monotonic() - t0)

        except Exception as e:
            last_err = str(e)
            retries_today += 1
            log.warning("hint extract failed: %s", e)
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

    if not cfg.hints.llm.enabled:
        log.info("hints LLM disabled; exiting")
        return

    shutdown = daemon_util.make_shutdown()
    run(cfg, shutdown)


if __name__ == "__main__":
    main()
