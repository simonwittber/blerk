CREATE TABLE IF NOT EXISTS repository (
    id             INTEGER PRIMARY KEY,
    path           TEXT    NOT NULL UNIQUE,
    url            TEXT    NOT NULL DEFAULT '',
    last_commit_at INTEGER NOT NULL DEFAULT 0
);

-- Content-addressed: one row per unique file content (keyed by hash).
-- Symbols and code blocks hang off this table, so identical content
-- across branches or worktrees is symbolized and embedded exactly once.
CREATE TABLE IF NOT EXISTS files (
    id   INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL DEFAULT 0
);

-- One row per physical file location on disk.
-- Many file_paths rows may point to the same files row when content is identical.
CREATE TABLE IF NOT EXISTS file_paths (
    id      INTEGER PRIMARY KEY,
    path    TEXT    NOT NULL UNIQUE,
    mtime   INTEGER NOT NULL DEFAULT 0,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_file_paths_file ON file_paths(file_id);

-- One row per logical file version: repo + relative path + branch.
-- file_id references the content. rel_path is relative to the worktree root.
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

-- Queued when new file content is seen; triggers symbolization once per unique hash.
CREATE TABLE IF NOT EXISTS symbol_queue (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL DEFAULT 'pending',
    priority  INTEGER NOT NULL DEFAULT 1,
    attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
    error     TEXT
);

-- Queued when a new physical path appears; triggers git log enrichment per path.
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

-- New content triggers stale findings for symbols on that content.
CREATE TRIGGER IF NOT EXISTS findings_stale_on_content_change
AFTER UPDATE ON file_paths WHEN OLD.file_id != NEW.file_id
BEGIN
    UPDATE findings SET stale = 1
    WHERE symbol_id IN (SELECT id FROM symbols WHERE file_id = OLD.file_id);
END;

-- New file content: queue symbolization once.
CREATE TRIGGER IF NOT EXISTS files_after_insert
AFTER INSERT ON files BEGIN
    INSERT INTO symbol_queue(file_id) VALUES (NEW.id);
END;

-- New physical path: queue git enrichment.
CREATE TRIGGER IF NOT EXISTS file_paths_after_insert
AFTER INSERT ON file_paths BEGIN
    INSERT INTO git_queue(file_path_id) VALUES (NEW.id);
END;

-- Path now points to different content: queue re-symbolization for the new content.
CREATE TRIGGER IF NOT EXISTS file_paths_after_update_content
AFTER UPDATE ON file_paths WHEN OLD.file_id != NEW.file_id BEGIN
    INSERT OR IGNORE INTO symbol_queue(file_id, queued_at) VALUES (NEW.file_id, unixepoch());
END;

-- Last path for a content row deleted: remove the content row too.
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
