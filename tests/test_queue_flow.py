from __future__ import annotations

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
    conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES('h', 0)")
    fid = int(conn.execute("SELECT id FROM files WHERE hash='h'").fetchone()[0])
    conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (path, fid))
    return fid


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
        conn.execute("DELETE FROM file_paths WHERE file_id=?", (fid,))
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


