from __future__ import annotations

import logging
import os
import sqlite3
import struct
import threading
import time
from datetime import datetime

import httpx

from blerk import config, coordinator, daemon_util, db


QUEUE = "code_block_embed_queue"
TARGET_COL = "block_id"
DAEMON = "embedder"

log = logging.getLogger("embedder")

_client = httpx.Client(timeout=120.0)
_st_model = None
_st_lock = threading.Lock()


def _get_sentence_transformer(model: str, device: str, cache_dir: str):
    global _st_model
    if _st_model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError("sentence-transformers not installed; install with: pip install sentence-transformers")
        cache_path = os.path.expanduser(cache_dir)
        os.makedirs(cache_path, exist_ok=True)
        _st_model = SentenceTransformer(model, device=device, cache_folder=cache_path)
    return _st_model


def embed_ollama(endpoint: str, model: str, text: str) -> list[float]:
    r = _client.post(
        endpoint + "/api/embeddings",
        json={"model": model, "prompt": text},
    )
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text}")
    return r.json()["embedding"]


def embed_sentence_transformers(model: str, device: str, cache_dir: str, text: str) -> list[float]:
    st = _get_sentence_transformer(model, device, cache_dir)
    with _st_lock:
        vecs = st.encode([text], convert_to_numpy=True)
    return vecs[0].tolist()


def embed(backend: str, endpoint: str, model: str, text: str, device: str = "auto", cache_dir: str = "~/.cache/huggingface") -> list[float]:
    if backend == "ollama":
        return embed_ollama(endpoint, model, text)
    elif backend == "sentence-transformers":
        return embed_sentence_transformers(model, device, cache_dir, text)
    else:
        raise RuntimeError(f"unknown embedding backend: {backend}")


def embed_with_truncation(backend: str, endpoint: str, model: str, text: str, device: str = "auto", cache_dir: str = "~/.cache/huggingface") -> list[float]:
    max_iters = 20
    iters = 0
    while text:
        if iters >= max_iters:
            raise RuntimeError("embed_with_truncation exceeded max iterations")
        iters += 1
        try:
            return embed(backend, endpoint, model, text, device, cache_dir)
        except httpx.TimeoutException as e:
            raise RuntimeError(f"embed timed out: {e}") from e
        except RuntimeError as e:
            if "context length" not in str(e).lower():
                raise
            text = text[: len(text) // 2]
    raise RuntimeError("text truncated to empty string")


def to_float32_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)



def run(cfg: config.Config, shutdown: threading.Event, silent: bool = False) -> None:
    conn = db.open_db(cfg.db.path)
    try:
        db.recover_orphans(conn, QUEUE)
    except sqlite3.Error as e:
        log.warning("recover orphans: %s", e)

    client = coordinator.CoordinatorClient(QUEUE, cfg.db.path)
    poll = cfg.embedder.poll_ms / 1000.0

    processed_today = 0
    retries_today = 0
    failures_today = 0
    rate_window: list[float] = []
    day_start = daemon_util.beginning_of_day(datetime.now())

    while not shutdown.is_set():
        status = "idle"
        last_err = ""

        try:
            rows = db.claim_batch(conn, QUEUE, TARGET_COL, cfg.embedder.batch_size)
        except sqlite3.Error as e:
            log.warning("claim batch: %s", e)
            status = "error"
            last_err = str(e)
            rows = []

        if rows:
            status = "running"
            for row in rows:
                blk_row = conn.execute(
                    "SELECT cb.content, cb.start_line, cb.block_index,"
                    " s.id, s.name, COALESCE(s.description, ''), f.path,"
                    " COALESCE(s.params, ''), s.kind, s.file_id, s.line, s.nesting_depth"
                    " FROM code_blocks cb"
                    " JOIN symbols s ON s.id = cb.symbol_id"
                    " JOIN files f ON f.id = s.file_id"
                    " WHERE cb.id=?",
                    (row.target_id,),
                ).fetchone()
                if not blk_row:
                    try:
                        db.mark_done(conn, QUEUE, row.id)
                    except sqlite3.Error as e:
                        log.warning("mark done %s %d: %s", QUEUE, row.id, e)
                    continue

                (block_content, block_start, block_index,
                 sym_id, name, description, path,
                 params, kind, file_id, line, nesting_depth) = blk_row

                parent_row = None
                if nesting_depth and nesting_depth > 0:
                    parent_row = conn.execute(
                        "SELECT name FROM symbols "
                        "WHERE file_id=? AND kind IN ('class','struct','interface','enum','type') "
                        "AND line<=? AND (end_line IS NULL OR end_line>=?) "
                        "AND nesting_depth=? "
                        "ORDER BY line DESC LIMIT 1",
                        (file_id, line, line, nesting_depth - 1),
                    ).fetchone()
                parent_class = parent_row[0] if parent_row else ""

                ns_row = conn.execute(
                    "SELECT value FROM symbol_tags WHERE symbol_id=? AND key='namespace'",
                    (sym_id,),
                ).fetchone()
                namespace = ns_row[0] if ns_row else ""

                callers = conn.execute(
                    "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.caller_id "
                    "WHERE r.callee_id=? LIMIT 10",
                    (sym_id,),
                ).fetchall()
                callees = conn.execute(
                    "SELECT s.name FROM symbol_refs r JOIN symbols s ON s.id = r.callee_id "
                    "WHERE r.caller_id=? LIMIT 10",
                    (sym_id,),
                ).fetchall()

                sig = f"({params})" if params else ""
                ns_prefix = f"{namespace}." if namespace else ""
                cls_prefix = f"{parent_class}." if parent_class else ""
                block_desc = ""
                if block_index == 0:
                    block_desc = description
                else:
                    bd_row = conn.execute(
                        "SELECT COALESCE(description, '') FROM code_blocks WHERE id=?",
                        (row.target_id,),
                    ).fetchone()
                    block_desc = bd_row[0] if bd_row else ""

                parts = [f"{ns_prefix}{cls_prefix}{name}{sig}"]
                if block_desc:
                    parts.append(": ")
                    parts.append(block_desc)
                parts.append(f"\nin {path}")
                if callers:
                    parts.append("\ncallers: " + ", ".join(r[0] for r in callers))
                if callees:
                    parts.append("\ncallees: " + ", ".join(r[0] for r in callees))
                if block_content and kind in ("function", "method"):
                    parts.append("\n\n")
                    parts.append(block_content)
                text = "".join(parts)

                t0 = time.monotonic()
                try:
                    vec = embed_with_truncation(
                        cfg.embedder.backend, cfg.embedder.endpoint, cfg.embedder.model, text,
                        cfg.embedder.device, cfg.embedder.cache_dir
                    )
                except Exception as e:
                    log.warning("embed %s: %s", name, e)
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), cfg.embedder.max_retries)
                    except sqlite3.Error as req_err:
                        log.warning("requeue %s %d: %s", QUEUE, row.id, req_err)
                        failed = False
                    retries_today += 1
                    if failed:
                        failures_today += 1
                    continue

                blob = to_float32_blob(vec)

                try:
                    conn.execute(
                        "INSERT INTO embeddings(block_id, model, vector, embedded_at) "
                        "VALUES(?, ?, ?, unixepoch()) "
                        "ON CONFLICT(block_id, model) DO UPDATE SET "
                        "vector = excluded.vector, "
                        "embedded_at = excluded.embedded_at",
                        (row.target_id, cfg.embedder.model, blob),
                    )
                except sqlite3.Error as e:
                    try:
                        failed = db.requeue(conn, QUEUE, row.id, str(e), cfg.embedder.max_retries)
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

                if not silent:
                    log.info("%s: %s, %s in %s", DAEMON, daemon_util.fmt_duration(time.monotonic() - t0), name, path)

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
