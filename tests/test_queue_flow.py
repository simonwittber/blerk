from __future__ import annotations

import hashlib
import sqlite3

import pytest

from blerk import config, db
from blerk.symbols.types import Symbol
from blerk_cmd.symbolizer import process_symbols
from blerk_cmd.watch_folder import delete_file, upsert_file


def _cfg() -> config.Config:
    cfg = config.defaults()
    cfg.symbolizer.min_describe_lines = 0
    return cfg


def _insert_file(conn, path: str = "a/b.py") -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (path, 0, "h"),
    )
    return int(cur.lastrowid)


def _queue_count(conn, queue: str, status: str = "pending") -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {queue} WHERE status=?", (status,)
        ).fetchone()[0]
    )


class TestFileEventQueueing:
    def test_new_file_triggers_symbol_and_git_queue(self, conn):
        _insert_file(conn, "src/main.py")
        assert _queue_count(conn, "symbol_queue") == 1
        assert _queue_count(conn, "git_queue") == 1

    def test_upsert_same_file_twice_no_extra_queue_entry(self, conn, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("x = 1", encoding="utf-8")
        upsert_file(conn, str(f))
        assert _queue_count(conn, "symbol_queue") == 1
        upsert_file(conn, str(f))
        assert _queue_count(conn, "symbol_queue") == 1

    def test_changed_content_triggers_resymbolization(self, conn, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("x = 1", encoding="utf-8")
        upsert_file(conn, str(f))
        assert _queue_count(conn, "symbol_queue") == 1
        f.write_text("x = 2", encoding="utf-8")
        upsert_file(conn, str(f))
        assert _queue_count(conn, "symbol_queue") == 2

    def test_same_content_no_resymbolization(self, conn, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("x = 1", encoding="utf-8")
        upsert_file(conn, str(f))
        upsert_file(conn, str(f))
        assert _queue_count(conn, "symbol_queue") == 1

    def test_delete_file_removes_from_db(self, conn, tmp_path):
        f = tmp_path / "gone.py"
        f.write_text("pass", encoding="utf-8")
        upsert_file(conn, str(f))
        assert int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]) == 1
        delete_file(conn, str(f))
        assert int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]) == 0


class TestPipelineIntegrity:
    def test_new_file_symbolize_creates_embed_queue(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        assert _queue_count(conn, "code_block_embed_queue") >= 1

    def test_delete_file_cascades_embed_queue(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        assert _queue_count(conn, "code_block_embed_queue") >= 1
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        assert _queue_count(conn, "code_block_embed_queue") == 0

    def test_changed_symbol_replaces_embed_queue_entry(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        process_symbols(conn, _cfg(), row, "a/b.py",
                        [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")], [])
        conn.execute("DELETE FROM code_block_embed_queue")
        process_symbols(conn, _cfg(), row, "a/b.py",
                        [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): return 1")], [])
        assert _queue_count(conn, "code_block_embed_queue") == 1

    def test_unchanged_symbol_no_embed_queue_after_clear(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        conn.execute("DELETE FROM code_block_embed_queue")
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        assert _queue_count(conn, "code_block_embed_queue") == 0


class TestRestartSafety:
    def test_recover_orphans_resets_symbol_queue(self, conn):
        fid = _insert_file(conn)
        conn.execute("UPDATE symbol_queue SET status='processing' WHERE file_id=?", (fid,))
        assert _queue_count(conn, "symbol_queue", "processing") == 1
        db.recover_orphans(conn, "symbol_queue")
        assert _queue_count(conn, "symbol_queue", "processing") == 0
        assert _queue_count(conn, "symbol_queue", "pending") == 1

    def test_recover_orphans_resets_embed_queue(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        conn.execute("UPDATE code_block_embed_queue SET status='processing'")
        assert _queue_count(conn, "code_block_embed_queue", "processing") == 1
        db.recover_orphans(conn, "code_block_embed_queue")
        assert _queue_count(conn, "code_block_embed_queue", "processing") == 0
        assert _queue_count(conn, "code_block_embed_queue", "pending") == 1

    def test_resymbolize_with_content_hash_no_new_embed_entries(self, conn):
        """Key restart-safety invariant: unchanged symbols must not re-queue embeddings."""
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        # Simulate embedder completing its work.
        conn.execute("DELETE FROM code_block_embed_queue")
        # Re-symbolize (e.g. after orphan recovery re-queues the file).
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        assert _queue_count(conn, "code_block_embed_queue") == 0

    def test_resymbolize_null_content_hash_treated_as_changed(self, conn):
        """NULL content_hash (pre-backfill state) causes re-embedding on every re-symbolize."""
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        conn.execute("UPDATE symbols SET content_hash=NULL WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM code_block_embed_queue")
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        assert _queue_count(conn, "code_block_embed_queue") >= 1

    def test_pending_embed_entries_survive_recover_orphans(self, conn):
        fid = _insert_file(conn)
        row = db.QueueRow(id=1, target_id=fid)
        syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")]
        process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
        initial = _queue_count(conn, "code_block_embed_queue", "pending")
        assert initial >= 1
        db.recover_orphans(conn, "code_block_embed_queue")
        assert _queue_count(conn, "code_block_embed_queue", "pending") == initial


class TestMigrationContentHashBackfill:
    def _build_pre_v7_db(self, db_path: str, snippet: str) -> None:
        """Create a minimal v6 database with a symbol row containing snippet data."""
        raw = sqlite3.connect(db_path)
        raw.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL DEFAULT 0);
            INSERT INTO schema_version VALUES(6);

            CREATE TABLE files (
                id    INTEGER PRIMARY KEY,
                path  TEXT NOT NULL UNIQUE,
                mtime INTEGER NOT NULL,
                size  INTEGER NOT NULL DEFAULT 0,
                hash  TEXT NOT NULL
            );

            CREATE TABLE symbols (
                id            INTEGER PRIMARY KEY,
                file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                kind          TEXT NOT NULL,
                line          INTEGER NOT NULL,
                end_line      INTEGER,
                snippet       TEXT,
                params        TEXT,
                nesting_depth INTEGER NOT NULL DEFAULT 0,
                param_count   INTEGER NOT NULL DEFAULT 0,
                description   TEXT,
                described_at  INTEGER,
                ext           TEXT
            );

            CREATE TABLE embeddings (
                id          INTEGER PRIMARY KEY,
                symbol_id   INTEGER NOT NULL,
                model       TEXT NOT NULL,
                vector      BLOB NOT NULL,
                embedded_at INTEGER NOT NULL
            );

            CREATE TABLE fingerprints (
                symbol_id INTEGER NOT NULL,
                kind      TEXT NOT NULL,
                value     TEXT NOT NULL,
                PRIMARY KEY (symbol_id, kind)
            );

            CREATE TABLE fingerprint_queue (
                id        INTEGER PRIMARY KEY,
                symbol_id INTEGER NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                priority  INTEGER NOT NULL DEFAULT 1,
                attempts  INTEGER NOT NULL DEFAULT 0,
                queued_at INTEGER NOT NULL DEFAULT (unixepoch()),
                error     TEXT
            );
        """)
        raw.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", ("a/b.py", 0, "h"))
        fid = raw.execute("SELECT last_insert_rowid()").fetchone()[0]
        raw.execute(
            "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
            (fid, "foo", "function", 1, 5, snippet),
        )
        raw.commit()
        raw.close()

    def test_migrate7_backfills_content_hash_from_snippet(self, tmp_path):
        snippet = "def foo(): pass"
        db_path = str(tmp_path / "pre_v7.db")
        self._build_pre_v7_db(db_path, snippet)

        conn = db.open_db(db_path)
        try:
            row = conn.execute("SELECT content_hash FROM symbols WHERE name='foo'").fetchone()
            assert row is not None
            assert row[0] is not None
            expected = hashlib.sha256(snippet.encode("utf-8", errors="replace")).hexdigest()[:16]
            assert row[0] == expected
        finally:
            conn.close()

    def test_migrate7_backfill_means_resymbolize_is_unchanged(self, tmp_path):
        """After backfill, re-symbolizing the same content must not re-queue embeddings."""
        snippet = "def foo(): pass"
        db_path = str(tmp_path / "pre_v7.db")
        self._build_pre_v7_db(db_path, snippet)

        conn = db.open_db(db_path)
        try:
            conn.execute("DELETE FROM code_block_embed_queue")
            fid = int(conn.execute("SELECT id FROM files WHERE path='a/b.py'").fetchone()[0])
            row = db.QueueRow(id=1, target_id=fid)
            syms = [Symbol(name="foo", kind="function", line=1, end_line=5, snippet=snippet)]
            process_symbols(conn, _cfg(), row, "a/b.py", syms, [])
            assert _queue_count(conn, "code_block_embed_queue") == 0
        finally:
            conn.close()
