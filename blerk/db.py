from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import NamedTuple

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS repository (
    id             INTEGER PRIMARY KEY,
    path           TEXT    NOT NULL UNIQUE,
    url            TEXT    NOT NULL DEFAULT '',
    last_commit_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id   INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS file_paths (
    id      INTEGER PRIMARY KEY,
    path    TEXT    NOT NULL UNIQUE,
    mtime   INTEGER NOT NULL DEFAULT 0,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_file_paths_file ON file_paths(file_id);

CREATE TABLE IF NOT EXISTS git_files (
    id              INTEGER PRIMARY KEY,
    repository_id   INTEGER NOT NULL REFERENCES repository(id) ON DELETE CASCADE,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    rel_path        TEXT    NOT NULL DEFAULT '',
    git_branch      TEXT    NOT NULL DEFAULT '',
    git_commit      TEXT,
    git_author      TEXT,
    git_enriched_at INTEGER,
    UNIQUE (repository_id, rel_path, git_branch)
);

CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    end_line       INTEGER,
    content_hash   TEXT,
    params         TEXT,
    nesting_depth  INTEGER NOT NULL DEFAULT 0,
    param_count    INTEGER NOT NULL DEFAULT 0,
    description    TEXT,
    described_at   INTEGER,
    ext            TEXT
);

CREATE TABLE IF NOT EXISTS code_blocks (
    id           INTEGER PRIMARY KEY,
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    block_index  INTEGER NOT NULL,
    content      TEXT    NOT NULL,
    content_hash TEXT,
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    description  TEXT,
    described_at INTEGER,
    UNIQUE (symbol_id, block_index)
);

CREATE INDEX IF NOT EXISTS idx_code_blocks_symbol ON code_blocks(symbol_id);

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
    id           INTEGER PRIMARY KEY,
    content_hash TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    vector       BLOB    NOT NULL,
    embedded_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_queue (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS git_queue (
    id           INTEGER PRIMARY KEY,
    file_path_id INTEGER NOT NULL REFERENCES file_paths(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     INTEGER NOT NULL DEFAULT 1,
    attempts     INTEGER NOT NULL DEFAULT 0,
    queued_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    error        TEXT
);

CREATE TABLE IF NOT EXISTS code_block_describe_queue (
    id        INTEGER PRIMARY KEY,
    block_id  INTEGER NOT NULL REFERENCES code_blocks(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

CREATE TABLE IF NOT EXISTS code_block_embed_queue (
    id        INTEGER PRIMARY KEY,
    block_id  INTEGER NOT NULL REFERENCES code_blocks(id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS analyzers (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS analyzer_rules (
    id          INTEGER PRIMARY KEY,
    analyzer_id INTEGER NOT NULL REFERENCES analyzers(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    severity    TEXT    NOT NULL,
    description TEXT    NOT NULL,
    UNIQUE (analyzer_id, name)
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    symbol_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    rule_id     INTEGER NOT NULL REFERENCES analyzer_rules(id) ON DELETE CASCADE,
    message     TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    stale       BOOLEAN NOT NULL DEFAULT 0,
    analyzed_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (symbol_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_findings_symbol ON findings(symbol_id);

CREATE TRIGGER IF NOT EXISTS findings_stale_on_content_change
AFTER UPDATE ON file_paths WHEN OLD.file_id != NEW.file_id
BEGIN
    UPDATE findings SET stale = 1
    WHERE symbol_id IN (SELECT id FROM symbols WHERE file_id = OLD.file_id);
END;

CREATE TRIGGER IF NOT EXISTS files_after_insert
AFTER INSERT ON files BEGIN
    INSERT OR IGNORE INTO symbol_queue(file_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS file_paths_after_insert
AFTER INSERT ON file_paths BEGIN
    INSERT INTO git_queue(file_path_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS file_paths_after_update_content
AFTER UPDATE ON file_paths WHEN OLD.file_id != NEW.file_id BEGIN
    INSERT OR IGNORE INTO symbol_queue(file_id, queued_at) VALUES (NEW.file_id, unixepoch());
END;

CREATE TRIGGER IF NOT EXISTS file_paths_after_delete_orphan
AFTER DELETE ON file_paths BEGIN
    DELETE FROM files WHERE id = OLD.file_id
    AND NOT EXISTS (SELECT 1 FROM file_paths WHERE file_id = OLD.file_id);
END;

CREATE TRIGGER IF NOT EXISTS code_blocks_embed_insert
AFTER INSERT ON code_blocks
BEGIN
    INSERT INTO code_block_embed_queue(block_id) VALUES (NEW.id);
END;

CREATE TRIGGER IF NOT EXISTS code_blocks_describe_insert
AFTER INSERT ON code_blocks
BEGIN
    INSERT INTO code_block_describe_queue(block_id) VALUES (NEW.id);
END;

CREATE TABLE IF NOT EXISTS queue_history (
    id          INTEGER PRIMARY KEY,
    queue_name  TEXT    NOT NULL,
    queue_count INTEGER NOT NULL,
    timestamp   INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_queue_history_queue_time ON queue_history(queue_name, timestamp DESC);

CREATE TABLE IF NOT EXISTS transcripts (
    id        INTEGER PRIMARY KEY,
    path      TEXT    NOT NULL DEFAULT '',
    cwd       TEXT    NOT NULL DEFAULT '',
    content   TEXT    NOT NULL DEFAULT '',
    stored_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS knowledge (
    id             INTEGER PRIMARY KEY,
    concept        TEXT    NOT NULL,
    pattern        TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    source         TEXT    NOT NULL DEFAULT 'explicit',
    importance     INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL DEFAULT (unixepoch()),
    queue_id       INTEGER,
    surfaced_count INTEGER NOT NULL DEFAULT 0,
    refined_at     INTEGER,
    suppressed_at  INTEGER,
    fact_checked_at INTEGER
);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    id           INTEGER PRIMARY KEY,
    knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    vector       BLOB    NOT NULL,
    model        TEXT    NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS knowledge_embed_queue (
    id           INTEGER PRIMARY KEY,
    knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending',
    queued_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    retries      INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_dedup_queue (
    id           INTEGER PRIMARY KEY,
    knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending',
    queued_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    retries      INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_contradictions (
    id          INTEGER PRIMARY KEY,
    id_a        INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    id_b        INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    detected_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    description,
    content=symbols,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS symbols_fts_insert
AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, description)
    VALUES (NEW.id, NEW.name, NEW.description);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_update
AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, description)
    VALUES ('delete', OLD.id, OLD.name, OLD.description);
    INSERT INTO symbols_fts(rowid, name, description)
    VALUES (NEW.id, NEW.name, NEW.description);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_delete
AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, description)
    VALUES ('delete', OLD.id, OLD.name, OLD.description);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS code_blocks_fts USING fts5(
    content,
    content=code_blocks,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS code_blocks_fts_insert
AFTER INSERT ON code_blocks BEGIN
    INSERT INTO code_blocks_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;

CREATE TRIGGER IF NOT EXISTS code_blocks_fts_update
AFTER UPDATE ON code_blocks BEGIN
    INSERT INTO code_blocks_fts(code_blocks_fts, rowid, content)
    VALUES ('delete', OLD.id, OLD.content);
    INSERT INTO code_blocks_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;

CREATE TRIGGER IF NOT EXISTS code_blocks_fts_delete
AFTER DELETE ON code_blocks BEGIN
    INSERT INTO code_blocks_fts(code_blocks_fts, rowid, content)
    VALUES ('delete', OLD.id, OLD.content);
END;
"""


class QueueRow(NamedTuple):
    id: int
    target_id: int


@dataclass
class Heartbeat:
    daemon: str
    status: str
    queue_depth: int
    processed_today: int
    retries_today: int
    failures_today: int
    rate_per_minute: float
    eta_seconds: int | None
    last_error: str = ""


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


_CURRENT_VERSION = 21


def _get_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES(?)", (version,))


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    version = _get_version(conn)
    if version == 0:
        _set_version(conn, _CURRENT_VERSION)
    elif version < _CURRENT_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is too old to migrate automatically. "
            "Delete the database file and let blerk recreate it."
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_content_hash_model"
        " ON embeddings(content_hash, model)"
    )


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


def mark_queue_done(conn: sqlite3.Connection, queue: str, id: int) -> None:
    with _write_lock:
        conn.execute(f"UPDATE {queue} SET status='done' WHERE id=?", (id,))


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


def get_or_create_rule(
    conn: sqlite3.Connection,
    analyzer_name: str,
    rule_name: str,
    severity: str,
    description: str,
) -> int:
    with _write_lock:
        conn.execute(
            "INSERT INTO analyzers(name) VALUES(?) ON CONFLICT(name) DO NOTHING",
            (analyzer_name,),
        )
        row = conn.execute("SELECT id FROM analyzers WHERE name=?", (analyzer_name,)).fetchone()
        analyzer_id = row[0]
        conn.execute(
            "INSERT INTO analyzer_rules(analyzer_id, name, severity, description)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(analyzer_id, name) DO UPDATE SET"
            " severity=excluded.severity, description=excluded.description",
            (analyzer_id, rule_name, severity, description),
        )
        row = conn.execute(
            "SELECT id FROM analyzer_rules WHERE analyzer_id=? AND name=?",
            (analyzer_id, rule_name),
        ).fetchone()
        return row[0]


def ensure_analyzers(conn: sqlite3.Connection, analyzers) -> dict[str, dict[str, int]]:
    """Upsert analyzers and their rules. Returns {analyzer_name: {rule_name: rule_id}}."""
    result: dict[str, dict[str, int]] = {}
    with _write_lock:
        for analyzer in analyzers:
            conn.execute(
                "INSERT INTO analyzers(name, description) VALUES(?, ?)"
                " ON CONFLICT(name) DO UPDATE SET description=excluded.description",
                (analyzer.name, getattr(analyzer, "description", "") or ""),
            )
            row = conn.execute("SELECT id FROM analyzers WHERE name=?", (analyzer.name,)).fetchone()
            analyzer_id = row[0]
            result[analyzer.name] = {}
            for rule in analyzer.rules:
                conn.execute(
                    "INSERT INTO analyzer_rules(analyzer_id, name, severity, description)"
                    " VALUES(?, ?, ?, ?)"
                    " ON CONFLICT(analyzer_id, name) DO UPDATE SET"
                    " severity=excluded.severity, description=excluded.description",
                    (analyzer_id, rule.name, rule.severity, rule.description),
                )
                row = conn.execute(
                    "SELECT id FROM analyzer_rules WHERE analyzer_id=? AND name=?",
                    (analyzer_id, rule.name),
                ).fetchone()
                result[analyzer.name][rule.name] = row[0]
    return result


def write_heartbeat(conn: sqlite3.Connection, hb: Heartbeat) -> None:
    err_val: str | None = hb.last_error if hb.last_error else None
    eta: int | None = None
    eta_display: str | None = None
    if hb.eta_seconds is not None:
        eta = hb.eta_seconds
        eta_display = format_eta(hb.eta_seconds)

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
                hb.daemon,
                hb.status,
                hb.queue_depth,
                hb.processed_today,
                hb.retries_today,
                hb.failures_today,
                hb.rate_per_minute,
                eta,
                eta_display,
                err_val,
            ),
        )
