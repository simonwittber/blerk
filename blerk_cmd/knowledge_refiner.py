from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from blerk import config as _config_mod

DAEMON = "knowledge-refiner"

log = logging.getLogger(DAEMON)

_ACTION_RE = re.compile(r"\{.*?\}", re.DOTALL)

_COLS = ("id", "concept", "pattern", "body", "source", "created_at", "surfaced_count", "refined_at")

_RefinerFn = Callable[
    [sqlite3.Connection, "_config_mod.Config", dict, "_config_mod.KnowledgeRefiner"],
    tuple[str, dict | None],
]

_REFINERS: dict[str, _RefinerFn] = {}
_REFINER_DEFAULTS: dict[str, str] = {}


def _register(name: str, default_prompt: str = ""):
    def decorator(fn: _RefinerFn) -> _RefinerFn:
        _REFINERS[name] = fn
        if default_prompt:
            _REFINER_DEFAULTS[name] = default_prompt
        return fn
    return decorator


def _resolve_cfg(refiner_cfg: "_config_mod.KnowledgeRefiner") -> "_config_mod.KnowledgeRefiner":
    if not refiner_cfg.prompt_template and refiner_cfg.type in _REFINER_DEFAULTS:
        return dataclasses.replace(refiner_cfg, prompt_template=_REFINER_DEFAULTS[refiner_cfg.type])
    return refiner_cfg


@_register("task-filter")
def _task_filter(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    row: dict,
    refiner_cfg: "_config_mod.KnowledgeRefiner",
) -> tuple[str, dict | None]:
    from blerk_cmd.extract_knowledge import call_llm
    llm = cfg.knowledge.llm
    prompt = refiner_cfg.prompt_template.replace("{body}", row["body"])
    try:
        response = call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
    except Exception as e:
        log.warning("task-filter llm error for %s: %s", row["concept"], e)
        return "skip", None
    m = _ACTION_RE.search(response)
    if not m:
        return "skip", None
    try:
        result = json.loads(m.group())
    except json.JSONDecodeError:
        return "skip", None
    if result.get("action") == "delete":
        return "delete", None
    return "skip", None


_DEFAULT_FACT_CHECK_PROMPT = """\
You are verifying a knowledge claim against actual source code.

Claim: {body}

Relevant code from the codebase:
{snippets}

Does the code evidence support, refute, or is it inconclusive about the claim?
Reply with JSON only: {{"verdict": "confirmed"}} or {{"verdict": "refuted"}} or {{"verdict": "inconclusive"}}"""


@_register("fact-check", _DEFAULT_FACT_CHECK_PROMPT)
def _fact_check(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    row: dict,
    refiner_cfg: "_config_mod.KnowledgeRefiner",
) -> tuple[str, dict | None]:
    from blerk_cmd.query import snippet_search
    from blerk_cmd.extract_knowledge import call_llm

    pattern = row.get("pattern", "**")
    directory = re.sub(r"[\*\?\[\]].*", "", pattern).rstrip("/") or "."

    try:
        snippets = snippet_search(conn, cfg, row["body"], directory, n=10)
    except Exception as e:
        log.warning("fact-check search error for %s: %s", row["concept"], e)
        return "skip", None

    if not snippets:
        return "skip", None

    llm = cfg.knowledge.llm
    prompt = refiner_cfg.prompt_template.replace("{body}", row["body"]).replace("{snippets}", snippets)
    try:
        response = call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
        m = _ACTION_RE.search(response)
        if not m:
            return "skip", None
        result = json.loads(m.group())
    except Exception as e:
        log.warning("fact-check llm error for %s: %s", row["concept"], e)
        return "skip", None

    now = int(time.time())
    if result.get("verdict") == "refuted":
        return "update", {"suppressed_at": now, "fact_checked_at": now}
    return "update", {"fact_checked_at": now}


def _process_one(conn: sqlite3.Connection, cfg: "_config_mod.Config", active_refiners: list) -> bool:
    row_tuple = conn.execute(
        "SELECT id, concept, pattern, body, source, created_at, surfaced_count, refined_at"
        " FROM knowledge WHERE refined_at IS NULL LIMIT 1"
    ).fetchone()
    if not row_tuple:
        return False

    row = dict(zip(_COLS, row_tuple))

    for refiner_cfg in active_refiners:
        fn = _REFINERS.get(refiner_cfg.type)
        if not fn:
            log.warning("unknown refiner type: %s", refiner_cfg.type)
            continue
        action, fields = fn(conn, cfg, row, _resolve_cfg(refiner_cfg))
        if action == "delete":
            conn.execute("DELETE FROM knowledge WHERE id=?", (row["id"],))
            conn.commit()
            log.info("%s: deleted %s", DAEMON, row["concept"])
            return True
        elif action == "update" and fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE knowledge SET {sets}, refined_at=unixepoch() WHERE id=?",
                (*fields.values(), row["id"]),
            )
            conn.commit()
            return True

    conn.execute(
        "UPDATE knowledge SET refined_at=unixepoch() WHERE id=?", (row["id"],)
    )
    conn.commit()
    return True


def run(cfg: "_config_mod.Config", shutdown: threading.Event) -> None:
    from blerk import daemon_util, db
    conn = db.open_db(cfg.db.path)

    active_refiners = [r for r in cfg.knowledge.refiners if r.enabled]
    poll = cfg.knowledge.llm.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    day_start = daemon_util.beginning_of_day(datetime.now())

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        did_work = False
        try:
            did_work = _process_one(conn, cfg, active_refiners)
            if did_work:
                status = "running"
                processed_today += 1
                now = datetime.now()
                if (now - day_start).total_seconds() >= 86400:
                    day_start = daemon_util.beginning_of_day(now)
                    processed_today = 0
                    retries_today = 0
                    failures_today = 0
        except Exception as e:
            last_err = str(e)
            retries_today += 1
            log.warning("%s: %s", DAEMON, e)

        try:
            queue_depth = conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE refined_at IS NULL"
            ).fetchone()[0]
        except sqlite3.Error:
            queue_depth = 0

        try:
            db.write_heartbeat(conn, db.Heartbeat(
                DAEMON, status, queue_depth,
                processed_today, retries_today, failures_today,
                0.0, None, last_err,
            ))
        except sqlite3.Error as e:
            log.warning("heartbeat: %s", e)

        if not did_work:
            shutdown.wait(timeout=poll)

    try:
        conn.close()
    except sqlite3.Error:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a refiner pass over the knowledge table.")
    parser.add_argument("--refiner", default=None, help="run only this refiner type")
    parser.add_argument("--dry-run", action="store_true", help="print actions without writing to DB")
    parser.add_argument("--daemon", action="store_true", help="run as a long-running daemon")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    from blerk import config as _config, daemon_util
    cfg_path = args.config or _config.default_path()
    cfg = _config.load(cfg_path)

    if args.daemon:
        daemon_util.setup_logging(args.silent or cfg.silent)
        if not cfg.knowledge.llm.enabled:
            log.info("knowledge LLM disabled; exiting")
            return 0
        shutdown = daemon_util.make_shutdown()
        run(cfg, shutdown)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from blerk import db as _db
    conn = _db.open_db(cfg.db.path)

    active_refiners = [
        r for r in cfg.knowledge.refiners
        if r.enabled and (args.refiner is None or r.type == args.refiner)
    ]

    if not active_refiners:
        print("No enabled refiners found.", file=sys.stderr)
        return 0

    rows = conn.execute(
        "SELECT id, concept, pattern, body, source, created_at, surfaced_count, refined_at"
        " FROM knowledge"
    ).fetchall()

    deleted = 0
    updated = 0
    skipped = 0

    for row_tuple in rows:
        row = dict(zip(_COLS, row_tuple))
        for refiner_cfg in active_refiners:
            fn = _REFINERS.get(refiner_cfg.type)
            if not fn:
                log.warning("unknown refiner type: %s", refiner_cfg.type)
                continue
            action, fields = fn(conn, cfg, row, _resolve_cfg(refiner_cfg))
            if args.dry_run:
                print(f"[{refiner_cfg.type}] {action}: {row['concept']}")
            elif action == "delete":
                conn.execute("DELETE FROM knowledge WHERE id=?", (row["id"],))
                conn.commit()
                deleted += 1
            elif action == "update" and fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE knowledge SET {sets}, refined_at=unixepoch() WHERE id=?",
                    (*fields.values(), row["id"]),
                )
                conn.commit()
                updated += 1
            else:
                skipped += 1

    if not args.dry_run:
        print(f"deleted={deleted} updated={updated} skipped={skipped}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
