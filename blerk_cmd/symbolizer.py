from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from blerk import config, coordinator, daemon_util, db
from blerk.symbols.types import CallRef, Symbol


QUEUE = "symbol_queue"
TARGET_COL = "file_id"
DAEMON = "symbolizer"

log = logging.getLogger("symbolizer")



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
        ext_key = os.path.splitext(path)[1].lower()

        # Snapshot existing symbols keyed by (name, kind).
        existing: dict[tuple[str, str], tuple[int, str | None]] = {}
        for r in conn.execute(
            "SELECT id, name, kind, snippet FROM symbols WHERE file_id=?",
            (row.target_id,),
        ).fetchall():
            existing[(r[1], r[2])] = (r[0], r[3])

        extracted_keys = {(sym.name, sym.kind) for sym in syms}

        unchanged: list[tuple[Symbol, int]] = []
        changed: list[tuple[Symbol, int]] = []
        new_syms: list[Symbol] = []

        for sym in syms:
            key = (sym.name, sym.kind)
            if key in existing:
                old_id, old_snippet = existing[key]
                if old_snippet == sym.snippet:
                    unchanged.append((sym, old_id))
                else:
                    changed.append((sym, old_id))
            else:
                new_syms.append(sym)

        # Unchanged: update position metadata only; description and queues untouched.
        unchanged_ids: list[int] = []
        for sym, old_id in unchanged:
            conn.execute(
                "UPDATE symbols SET line=?, end_line=?, params=?, nesting_depth=?, param_count=? WHERE id=?",
                (sym.line, sym.end_line, sym.params or None, sym.nesting_depth, sym.param_count, old_id),
            )
            unchanged_ids.append(old_id)

        # Remove stale queue entries for unchanged symbols that are already embedded/fingerprinted.
        if unchanged_ids:
            ph = ",".join("?" * len(unchanged_ids))
            conn.execute(
                f"DELETE FROM embedding_queue WHERE symbol_id IN ({ph})"
                f" AND EXISTS (SELECT 1 FROM embeddings WHERE symbol_id = embedding_queue.symbol_id)",
                unchanged_ids,
            )
            conn.execute(
                f"DELETE FROM fingerprint_queue WHERE symbol_id IN ({ph})"
                f" AND EXISTS (SELECT 1 FROM fingerprints WHERE symbol_id = fingerprint_queue.symbol_id)",
                unchanged_ids,
            )

        # Changed: update all columns, clear description, re-queue for all pipelines.
        changed_ids: list[int] = []
        for sym, old_id in changed:
            conn.execute(
                "UPDATE symbols SET line=?, end_line=?, snippet=?, params=?, nesting_depth=?, param_count=?, "
                "description=NULL, described_at=NULL, ext=? WHERE id=?",
                (sym.line, sym.end_line, sym.snippet, sym.params or None,
                 sym.nesting_depth, sym.param_count, ext_key, old_id),
            )
            changed_ids.append(old_id)

        if changed_ids:
            ph = ",".join("?" * len(changed_ids))
            # Clear any existing pending queue entries before inserting fresh ones.
            conn.execute(f"DELETE FROM description_queue WHERE symbol_id IN ({ph})", changed_ids)
            conn.execute(f"DELETE FROM embedding_queue WHERE symbol_id IN ({ph})", changed_ids)
            conn.execute(f"DELETE FROM fingerprint_queue WHERE symbol_id IN ({ph})", changed_ids)
            conn.execute(
                f"INSERT INTO description_queue(symbol_id, priority) "
                f"SELECT id, 2 FROM symbols WHERE id IN ({ph}) AND kind IN ('function','method')",
                changed_ids,
            )
            conn.execute(
                f"INSERT INTO embedding_queue(symbol_id, priority) "
                f"SELECT id, 2 FROM symbols WHERE id IN ({ph}) AND kind != 'heading'",
                changed_ids,
            )
            conn.execute(
                f"INSERT INTO fingerprint_queue(symbol_id, priority) "
                f"SELECT id, 2 FROM symbols WHERE id IN ({ph}) AND kind IN ('function','method') AND snippet IS NOT NULL",
                changed_ids,
            )
            # Clear caller refs and external_refs so they are rebuilt below.
            conn.execute(f"DELETE FROM symbol_refs WHERE caller_id IN ({ph})", changed_ids)
            conn.execute(f"DELETE FROM external_refs WHERE caller_id IN ({ph})", changed_ids)

        # New: insert; triggers handle queue entries.
        inserted_ids: list[int] = []
        for sym in new_syms:
            cur = conn.execute(
                "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet, params, "
                "nesting_depth, param_count, description, described_at, ext) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,?) RETURNING id",
                (row.target_id, sym.name, sym.kind, sym.line, sym.end_line, sym.snippet,
                 sym.params or None, sym.nesting_depth, sym.param_count, ext_key),
            )
            r = cur.fetchone()
            if r is None:
                raise sqlite3.Error("insert symbols failed")
            inserted_ids.append(int(r[0]))

        # Bump new symbols to priority=2 when the file was previously known,
        # so they are processed ahead of the initial-scan backlog.
        if inserted_ids and existing:
            ph = ",".join("?" * len(inserted_ids))
            conn.execute(
                f"UPDATE embedding_queue SET priority=2 WHERE symbol_id IN ({ph}) AND status='pending'",
                inserted_ids,
            )
            conn.execute(
                f"UPDATE description_queue SET priority=2 WHERE symbol_id IN ({ph}) AND status='pending'",
                inserted_ids,
            )
            conn.execute(
                f"UPDATE fingerprint_queue SET priority=2 WHERE symbol_id IN ({ph}) AND status='pending'",
                inserted_ids,
            )

        # Delete removed symbols; CASCADE cleans up refs, embeddings, and queue entries.
        removed_ids = [eid for (name, kind), (eid, _) in existing.items()
                       if (name, kind) not in extracted_keys]
        if removed_ids:
            ph = ",".join("?" * len(removed_ids))
            conn.execute(f"DELETE FROM symbols WHERE id IN ({ph})", removed_ids)

        # Build combined name->id map for all surviving symbols.
        all_syms_with_ids: list[tuple[Symbol, int]] = (
            [(sym, old_id) for sym, old_id in unchanged]
            + [(sym, old_id) for sym, old_id in changed]
            + [(sym, new_id) for sym, new_id in zip(new_syms, inserted_ids)]
        )
        name_to_id: dict[str, int] = {sym.name: sid for sym, sid in all_syms_with_ids}

        # Update tags for all surviving symbols.
        tag_rows: list[tuple[int, str, str]] = []
        for sym, sid in all_syms_with_ids:
            for k, v in sym.tags.items():
                tag_rows.append((sid, k, v))
        if tag_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_tags(symbol_id, key, value) VALUES(?,?,?)",
                tag_rows,
            )

        # Rebuild refs for changed and new symbols only.
        active_caller_ids = set(changed_ids) | set(inserted_ids)
        if refs and active_caller_ids:
            for ref in refs:
                caller_id = name_to_id.get(ref.caller_name)
                if caller_id is None or caller_id not in active_caller_ids:
                    continue
                callee_id = name_to_id.get(ref.callee_name)
                if callee_id is None:
                    r = conn.execute(
                        "SELECT id FROM symbols WHERE (name=? OR name LIKE ?) "
                        "AND kind IN ('function','method') LIMIT 1",
                        (ref.callee_name, f"%.{ref.callee_name}"),
                    ).fetchone()
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

        # Promote external_refs that now resolve to newly inserted symbols.
        if inserted_ids:
            new_set = set(inserted_ids)
            for sym, sid in all_syms_with_ids:
                if sid not in new_set:
                    continue
                short = sym.short_name or sym.name.split(".")[-1]
                for lookup in {sym.name, short}:
                    ext_rows = conn.execute(
                        "SELECT id, caller_id FROM external_refs WHERE callee_name=?",
                        (lookup,),
                    ).fetchall()
                    for ext_id, ext_caller_id in ext_rows:
                        if ext_caller_id != sid:
                            try:
                                conn.execute(
                                    "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
                                    (ext_caller_id, sid),
                                )
                            except sqlite3.Error as e:
                                log.warning("promote external_ref %d->%d: %s", ext_caller_id, sid, e)
                        conn.execute("DELETE FROM external_refs WHERE id=?", (ext_id,))

        # Apply min_describe_lines filter.
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


def run(cfg: config.Config, shutdown: threading.Event, silent: bool = False) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    from blerk.symbols import treesitter_extractor
    extractor = treesitter_extractor.Extractor()

    client = coordinator.CoordinatorClient(QUEUE, cfg.db.path)
    poll = cfg.symbolizer.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = daemon_util.beginning_of_day(datetime.now())

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

                t0 = time.monotonic()
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

                if not silent:
                    log.info("%s: %s, %d symbol(s), %s", DAEMON, daemon_util.fmt_duration(time.monotonic() - t0), len(syms), path)

                now = datetime.now()
                if (now - day_start).total_seconds() >= 24 * 3600:
                    day_start = daemon_util.beginning_of_day(now)
                    processed_today = 0
                    retries_today = 0
                    failures_today = 0
                rate_window.append(time.monotonic())
                processed_today += 1

            client.notify("description_queue")
            client.notify("embedding_queue")
            client.notify("fingerprint_queue")

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
