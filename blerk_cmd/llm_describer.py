from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime

import httpx

from blerk import config, db
from blerk.symbols import types as symbols_types


QUEUE = "description_queue"
TARGET_COL = "symbol_id"
DAEMON = "llm-describer"

log = logging.getLogger("llm-describer")

_client = httpx.Client(timeout=30.0)


def describe(endpoint: str, model: str, api_key: str, prompt: str) -> str:
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
        raise RuntimeError(f"llm {r.status_code}: {r.text}")

    j = r.json()
    choices = j.get("choices", [])
    if not choices:
        raise RuntimeError("empty response from llm")
    return choices[0]["message"]["content"]


@dataclasses.dataclass
class SymbolInfo:
    id: int
    name: str
    kind: str
    path: str
    line: int
    end_line: int


_BRACKET_TABLE = str.maketrans({"{": "(", "}": ")"})


def _safe(s: str) -> str:
    return s.translate(_BRACKET_TABLE)


def build_prompt(sym: SymbolInfo, template: str, max_context_chars: int) -> str:
    try:
        ctx = symbols_types.build_context(sym.path, sym.line, sym.end_line, max_context_chars)
    except OSError as e:
        ctx = f"(source unavailable: {e})"
    return (
        template
        .replace("{kind}", _safe(sym.kind))
        .replace("{name}", _safe(sym.name))
        .replace("{path}", _safe(sym.path))
        .replace("{context}", _safe(ctx))
    )


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


def run(cfg: config.Config, llm: config.LLM, shutdown: threading.Event, daemon_name: str = DAEMON) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    poll = llm.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = beginning_of_day(datetime.now())

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        try:
            rows = db.claim_batch(conn, QUEUE, TARGET_COL, llm.batch_size)
        except sqlite3.Error as e:
            log.warning("claim batch: %s", e)
            status = "error"
            last_err = str(e)
            rows = []

        if rows:
            status = "running"
            for row in rows:
                sym_row = conn.execute(
                    "SELECT s.name, s.kind, f.path, COALESCE(s.line, 0), COALESCE(s.end_line, 0) "
                    "FROM symbols s JOIN files f ON f.id = s.file_id "
                    "WHERE s.id=?",
                    (row.target_id,),
                ).fetchone()
                if not sym_row:
                    try:
                        db.mark_done(conn, QUEUE, row.id)
                    except sqlite3.Error as e:
                        log.warning("mark done %s %d: %s", QUEUE, row.id, e)
                    continue

                sym = SymbolInfo(
                    id=row.target_id,
                    name=sym_row[0],
                    kind=sym_row[1],
                    path=sym_row[2],
                    line=int(sym_row[3]),
                    end_line=int(sym_row[4]),
                )

                prompt = build_prompt(sym, llm.prompt_template, llm.max_context_chars)

                try:
                    desc = describe(llm.endpoint, llm.model, llm.api_key, prompt)
                except Exception as e:
                    log.warning("describe %s: %s", sym.name, e)
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), llm.max_retries)
                    except sqlite3.Error as req_err:
                        log.warning("requeue %s %d: %s", QUEUE, row.id, req_err)
                        failed = False
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                try:
                    conn.execute(
                        "UPDATE symbols SET description=?, described_at=unixepoch() WHERE id=?",
                        (desc, sym.id),
                    )
                except sqlite3.Error as e:
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), llm.max_retries)
                    except sqlite3.Error as req_err:
                        log.warning("requeue %s %d: %s", QUEUE, row.id, req_err)
                        failed = False
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                try:
                    db.mark_done(conn, QUEUE, row.id)
                except sqlite3.Error as e:
                    log.warning("mark done %s %d: %s", QUEUE, row.id, e)

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
                daemon_name,
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
    parser.add_argument("--endpoint", default="", help="override LLM endpoint URL")
    parser.add_argument("--model", default="", help="override LLM model name")
    parser.add_argument("--daemon-name", default="", dest="daemon_name",
                        help="daemon name for status table (default: llm-describer)")
    args = parser.parse_args()

    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        log.error("load config: %s", e)
        sys.exit(1)

    llm = cfg.llm[0] if cfg.llm else config.defaults().llm[0]
    if args.endpoint:
        llm = config.dc_replace(llm, endpoint=args.endpoint)
    if args.model:
        llm = config.dc_replace(llm, model=args.model)
    daemon_name = args.daemon_name or DAEMON

    shutdown = threading.Event()

    def _sig(_signum, _frame):
        shutdown.set()

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    run(cfg, llm, shutdown, daemon_name)


if __name__ == "__main__":
    main()
