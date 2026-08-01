from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import NamedTuple

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY,
    path            TEXT    NOT NULL UNIQUE,
    mtime           INTEGER NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    hash            TEXT    NOT NULL,
    git_commit      TEXT,
    git_author      TEXT,
    git_branch      TEXT,
    git_enriched_at INTEGER
);

CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    end_line       INTEGER,
    snippet        TEXT,
    params         TEXT,
    nesting_depth  INTEGER NOT NULL DEFAULT 0,
    param_count    INTEGER NOT NULL DEFAULT 0,
    description    TEXT,
    described_at   INTEGER,
    ext            TEXT
);

CREATE TABLE IF NOT EXISTS symbol_tags (
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    key       TEXT    NOT NULL,
    value     TEXT    NOT NULL,
    PRIMARY KEY (symbol_id, key)
);

CREATE INDEX IF NOT EXISTS idx_symbol_tags_key_value ON symbol_tags(key, value);

CREATE INDEX IF NOT EXISTS idx_symbols_file_line ON symbols(file_id, line);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_kind_param_count ON symbols(kind, param_count);
CREATE INDEX IF NOT EXISTS idx_symbols_kind_nesting ON symbols(kind, nesting_depth);
CREATE INDEX IF NOT EXISTS idx_symbols_kind_file ON symbols(kind, file_id);

CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY,
    symbol_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    model       TEXT    NOT NULL,
    vector      BLOB    NOT NULL,
    embedded_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_symbol_model ON embeddings(symbol_id, model);

CREATE TABLE IF NOT EXISTS symbol_queue (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS git_queue (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS description_queue (
    id        INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS embedding_queue (
    id        INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    kind      TEXT    NOT NULL,
    value     TEXT    NOT NULL,
    PRIMARY KEY (symbol_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_kind_value ON fingerprints(kind, value);
CREATE INDEX IF NOT EXISTS idx_fingerprints_kind_symbol ON fingerprints(kind, symbol_id);

CREATE TABLE IF NOT EXISTS fingerprint_queue (
    id        INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS daemon_status (
    daemon           TEXT    PRIMARY KEY,
    status           TEXT    NOT NULL DEFAULT 'idle',
    queue_depth      INTEGER NOT NULL DEFAULT 0,
    processed_today  INTEGER NOT NULL DEFAULT 0,
    retries_today    INTEGER NOT NULL DEFAULT 0,
    failures_today   INTEGER NOT NULL DEFAULT 0,
    rate_per_minute  REAL    NOT NULL DEFAULT 0.0,
    eta_seconds      INTEGER,
    eta_display      TEXT,
    last_heartbeat   INTEGER NOT NULL DEFAULT (unixepoch()),
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS symbol_refs (
    id         INTEGER PRIMARY KEY,
    caller_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_symbol_refs_pair ON symbol_refs(caller_id, callee_id);
CREATE INDEX IF NOT EXISTS idx_symbol_refs_callee ON symbol_refs(callee_id);
CREATE INDEX IF NOT EXISTS idx_symbol_refs_caller ON symbol_refs(caller_id);

CREATE TABLE IF NOT EXISTS external_refs (
    id          INTEGER PRIMARY KEY,
    caller_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_name TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_refs_caller ON external_refs(caller_id);
CREATE INDEX IF NOT EXISTS idx_external_refs_callee_name ON external_refs(callee_name);

CREATE TRIGGER IF NOT EXISTS files_after_insert
AFTER INSERT ON files BEGIN
    INSERT INTO symbol_queue(file_id) VALUES (NEW.id);
    INSERT INTO git_queue(file_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS files_after_update
AFTER UPDATE ON files WHEN OLD.hash != NEW.hash BEGIN
    INSERT INTO symbol_queue(file_id, queued_at) VALUES (NEW.id, unixepoch());
END;

CREATE TRIGGER IF NOT EXISTS symbols_description_insert
AFTER INSERT ON symbols
WHEN NEW.kind IN ('function', 'method') AND NEW.description IS NULL
BEGIN
    INSERT INTO description_queue(symbol_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS symbols_embedding_insert
AFTER INSERT ON symbols
WHEN NEW.kind != 'heading'
BEGIN
    INSERT INTO embedding_queue(symbol_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS symbols_description_update
AFTER UPDATE ON symbols WHEN NEW.description IS NOT NULL AND OLD.description IS NULL BEGIN
    INSERT INTO embedding_queue(symbol_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fingerprint_insert
AFTER INSERT ON symbols
WHEN NEW.kind IN ('function', 'method') AND NEW.snippet IS NOT NULL
BEGIN
    INSERT INTO fingerprint_queue(symbol_id) VALUES (NEW.id);
END;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    description,
    snippet,
    content=symbols,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS symbols_fts_insert
AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, description, snippet)
    VALUES (NEW.id, NEW.name, NEW.description, NEW.snippet);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_update
AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, description, snippet)
    VALUES ('delete', OLD.id, OLD.name, OLD.description, OLD.snippet);
    INSERT INTO symbols_fts(rowid, name, description, snippet)
    VALUES (NEW.id, NEW.name, NEW.description, NEW.snippet);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_delete
AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, description, snippet)
    VALUES ('delete', OLD.id, OLD.name, OLD.description, OLD.snippet);
END;
"""


class QueueRow(NamedTuple):
    id: int
    target_id: int


def open_db(path: str, init_schema: bool = True) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)

    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except AttributeError:
        logger.warning("sqlite3 build lacks enable_load_extension; vec_distance_cosine unavailable")
    except Exception as e:
        logger.warning("failed to load sqlite-vec: %s", e)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if init_schema:
        _init_schema(conn)
    return conn


_CURRENT_VERSION = 4


def _get_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES(?)", (version,))


def _migrate_1(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
    for col, defn in [
        ("nesting_depth", "INTEGER NOT NULL DEFAULT 0"),
        ("param_count",   "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE symbols ADD COLUMN {col} {defn}")
    conn.execute("DROP TRIGGER IF EXISTS symbols_description_insert")
    conn.executescript(
        "CREATE TRIGGER IF NOT EXISTS symbols_description_insert "
        "AFTER INSERT ON symbols "
        "WHEN NEW.kind IN ('function', 'method') AND NEW.description IS NULL "
        "BEGIN INSERT INTO description_queue(symbol_id) VALUES (NEW.id); END;"
    )


def _migrate_2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO fingerprint_queue(symbol_id)
        SELECT s.id FROM symbols s
        WHERE s.kind IN ('function', 'method')
          AND s.snippet IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM fingerprints f WHERE f.symbol_id = s.id)
        """
    )


def _migrate_3(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "ext" not in existing:
        conn.execute("ALTER TABLE symbols ADD COLUMN ext TEXT")
    rows = conn.execute(
        "SELECT s.id, f.path FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.ext IS NULL"
    ).fetchall()
    updates = [(os.path.splitext(path)[1].lower(), sym_id) for sym_id, path in rows]
    if updates:
        conn.executemany("UPDATE symbols SET ext = ? WHERE id = ?", updates)


def _migrate_4(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
        CREATE INDEX IF NOT EXISTS idx_symbols_kind_param_count ON symbols(kind, param_count);
        CREATE INDEX IF NOT EXISTS idx_symbols_kind_nesting ON symbols(kind, nesting_depth);
        CREATE INDEX IF NOT EXISTS idx_symbols_kind_file ON symbols(kind, file_id);
        CREATE INDEX IF NOT EXISTS idx_fingerprints_kind_symbol ON fingerprints(kind, symbol_id);
    """)


_MIGRATIONS: dict[int, object] = {
    1: _migrate_1,
    2: _migrate_2,
    3: _migrate_3,
    4: _migrate_4,
}


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    version = _get_version(conn)
    if version < _CURRENT_VERSION:
        for v in range(version + 1, _CURRENT_VERSION + 1):
            fn = _MIGRATIONS.get(v)
            if fn:
                fn(conn)  # type: ignore[call-arg]
        _set_version(conn, _CURRENT_VERSION)


def claim_batch(conn: sqlite3.Connection, queue: str, target_col: str, n: int) -> list[QueueRow]:
    query = (
        f"UPDATE {queue} SET status='processing' "
        f"WHERE id IN ("
        f"SELECT id FROM {queue} WHERE status='pending' "
        f"ORDER BY priority DESC, queued_at ASC LIMIT ?"
        f") RETURNING id, {target_col}"
    )
    with _write_lock:
        cur = conn.execute(query, (n,))
        rows = cur.fetchall()
    return [QueueRow(int(r[0]), int(r[1])) for r in rows]


def mark_done(conn: sqlite3.Connection, queue: str, id: int) -> None:
    with _write_lock:
        conn.execute(f"DELETE FROM {queue} WHERE id=?", (id,))


def recover_orphans(conn: sqlite3.Connection, queue: str) -> None:
    with _write_lock:
        conn.execute(f"UPDATE {queue} SET status='pending' WHERE status='processing'")


def requeue(conn: sqlite3.Connection, queue: str, id: int, err_msg: str, max_attempts: int) -> bool:
    query = (
        f"UPDATE {queue} SET "
        f"status    = CASE WHEN attempts+1 >= ? THEN 'failed' ELSE 'pending' END, "
        f"priority  = 0, "
        f"attempts  = attempts+1, "
        f"queued_at = unixepoch(), "
        f"error     = ? "
        f"WHERE id=? "
        f"RETURNING status"
    )
    with _write_lock:
        cur = conn.execute(query, (max_attempts, err_msg, id))
        row = cur.fetchone()
    if row is None:
        return False
    return row[0] == "failed"


def format_eta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def write_heartbeat(
    conn: sqlite3.Connection,
    daemon: str,
    status: str,
    queue_depth: int,
    processed_today: int,
    retries_today: int,
    failures_today: int,
    rate_per_minute: float,
    eta_seconds: int | None,
    last_error: str,
) -> None:
    err_val: str | None = last_error if last_error else None
    eta: int | None = None
    eta_display: str | None = None
    if eta_seconds is not None:
        eta = eta_seconds
        eta_display = format_eta(eta_seconds)

    query = (
        "INSERT INTO daemon_status("
        "daemon, status, queue_depth, processed_today, retries_today, failures_today, "
        "rate_per_minute, eta_seconds, eta_display, last_heartbeat, last_error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch(), ?) "
        "ON CONFLICT(daemon) DO UPDATE SET "
        "status=excluded.status, "
        "queue_depth=excluded.queue_depth, "
        "processed_today=excluded.processed_today, "
        "retries_today=excluded.retries_today, "
        "failures_today=excluded.failures_today, "
        "rate_per_minute=excluded.rate_per_minute, "
        "eta_seconds=excluded.eta_seconds, "
        "eta_display=excluded.eta_display, "
        "last_heartbeat=excluded.last_heartbeat, "
        "last_error=excluded.last_error"
    )
    with _write_lock:
        conn.execute(
            query,
            (
                daemon,
                status,
                queue_depth,
                processed_today,
                retries_today,
                failures_today,
                rate_per_minute,
                eta,
                eta_display,
                err_val,
            ),
        )
