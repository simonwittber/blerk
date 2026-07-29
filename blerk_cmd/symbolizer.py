from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from blerk import config, db
from blerk.symbols import regexp_extractor
from blerk.symbols.types import CallRef, Symbol


QUEUE = "symbol_queue"
TARGET_COL = "file_id"
DAEMON = "symbolizer"

log = logging.getLogger("symbolizer")


class _RegexpAdapter:
    def extract(self, path: str) -> tuple[list[Symbol], list[CallRef]]:
        return regexp_extractor.extract_symbols(path), []


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


def process_symbols(
    conn: sqlite3.Connection,
    cfg: config.Config,
    row: db.QueueRow,
    path: str,
    syms: list[Symbol],
    refs: list[CallRef],
) -> None:
    conn.execute("BEGIN")
    try:
        # Snapshot descriptions before delete so unchanged snippets can be carried forward.
        old_descs: dict[tuple[str, str], tuple[str | None, int | None]] = {}
        for r_name, r_kind, r_snippet, r_desc, r_at in conn.execute(
            "SELECT name, kind, snippet, description, described_at FROM symbols WHERE file_id=?",
            (row.target_id,),
        ).fetchall():
            if r_desc is not None and r_snippet is not None:
                old_descs[(r_name, r_kind)] = (r_snippet, r_desc, r_at)

        conn.execute("DELETE FROM symbols WHERE file_id=?", (row.target_id,))

        inserted_ids: list[int] = []
        carried_ids: list[int] = []
        for sym in syms:
            old = old_descs.get((sym.name, sym.kind))
            carry_desc: str | None = None
            carry_at: int | None = None
            if old is not None and old[0] == sym.snippet:
                carry_desc, carry_at = old[1], old[2]

            cur = conn.execute(
                "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet, params, is_static, nesting_depth, param_count, description, described_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (row.target_id, sym.name, sym.kind, sym.line, sym.end_line, sym.snippet, sym.params or None, int(sym.is_static), sym.nesting_depth, sym.param_count, carry_desc, carry_at),
            )
            r = cur.fetchone()
            if r is None:
                raise sqlite3.Error("insert symbols failed")
            new_id = int(r[0])
            inserted_ids.append(new_id)
            if carry_desc is not None:
                carried_ids.append(new_id)

        if carried_ids:
            placeholders = ",".join("?" * len(carried_ids))
            conn.execute(
                f"DELETE FROM description_queue WHERE symbol_id IN ({placeholders})",
                carried_ids,
            )

        if refs:
            name_to_id: dict[str, int] = {}
            for i, sym in enumerate(syms):
                if i < len(inserted_ids):
                    name_to_id[sym.name] = inserted_ids[i]
            for ref in refs:
                caller_id = name_to_id.get(ref.caller_name)
                if caller_id is None:
                    continue
                callee_id = name_to_id.get(ref.callee_name)
                if callee_id is None:
                    cur = conn.execute(
                        "SELECT id FROM symbols WHERE name=? AND kind IN ('function','method') LIMIT 1",
                        (ref.callee_name,),
                    )
                    r = cur.fetchone()
                    if r is not None:
                        callee_id = int(r[0])
                if callee_id is not None and caller_id != callee_id:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
                            (caller_id, callee_id),
                        )
                    except sqlite3.Error as e:
                        log.warning("insert symbol_ref %d->%d: %s", caller_id, callee_id, e)
                elif callee_id is None:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO external_refs(caller_id, callee_name) VALUES(?,?)",
                            (caller_id, ref.callee_name),
                        )
                    except sqlite3.Error as e:
                        log.warning("insert external_ref %d->%s: %s", caller_id, ref.callee_name, e)

        min_lines = cfg.symbolizer.min_describe_lines
        if min_lines > 0:
            try:
                conn.execute(
                    "DELETE FROM description_queue "
                    "WHERE symbol_id IN ("
                    "SELECT id FROM symbols "
                    "WHERE file_id=? AND (COALESCE(end_line,0) - line) < ?"
                    ")",
                    (row.target_id, min_lines),
                )
            except sqlite3.Error as e:
                log.warning("delete short symbols from description_queue: %s", e)

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def run(cfg: config.Config, shutdown: threading.Event) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    if cfg.symbolizer.engine == "treesitter":
        from blerk.symbols import treesitter_extractor
        extractor: Any = treesitter_extractor.Extractor()
        log.info("symbolizer engine: treesitter")
    else:
        extractor = _RegexpAdapter()
        log.info("symbolizer engine: regexp")

    poll = cfg.symbolizer.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = beginning_of_day(datetime.now())

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        try:
            rows = db.claim_batch(conn, QUEUE, TARGET_COL, cfg.symbolizer.batch_size)
        except sqlite3.Error as e:
            log.warning("claim batch: %s", e)
            status = "error"
            last_err = str(e)
            rows = []

        if rows:
            status = "running"
            for row in rows:
                path_row = conn.execute(
                    "SELECT path FROM files WHERE id=?", (row.target_id,)
                ).fetchone()
                if not path_row:
                    try:
                        db.mark_done(conn, QUEUE, row.id)
                    except sqlite3.Error as e:
                        log.warning("mark done symbol_queue %d: %s", row.id, e)
                    continue
                path = path_row[0]

                try:
                    syms, refs = extractor.extract(path)
                    process_symbols(conn, cfg, row, path, syms, refs)
                except Exception as e:
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), cfg.symbolizer.max_retries)
                    except sqlite3.Error as req_err:
                        log.warning("requeue symbol_queue %d: %s", row.id, req_err)
                        failed = False
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                try:
                    db.mark_done(conn, QUEUE, row.id)
                except sqlite3.Error as e:
                    log.warning("mark done symbol_queue %d: %s", row.id, e)

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
