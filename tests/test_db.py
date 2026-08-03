from __future__ import annotations

import os
import time

import pytest

from blerk import db



def _insert_file(conn, path: str, hash_: str) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?, ?, ?)",
        (path, 0, hash_),
    )
    return int(cur.lastrowid)


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None and row[0] == name


def test_open_creates_schema(conn):
    for tbl in ["files", "symbols", "embeddings", "symbol_queue", "git_queue",
                "description_queue", "embedding_queue", "daemon_status", "symbol_refs",
                "analyzers", "analyzer_rules", "findings"]:
        assert _table_exists(conn, tbl), f"missing table {tbl}"


def test_open_pragmas_applied(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 30000


def test_open_idempotent(tmp_path):
    path = str(tmp_path / "test.db")
    c1 = db.open_db(path)
    c1.close()
    c2 = db.open_db(path)
    try:
        assert _table_exists(c2, "files")
    finally:
        c2.close()


def test_open_creates_parent_dir(tmp_path):
    path = str(tmp_path / "sub" / "nested" / "test.db")
    c = db.open_db(path)
    try:
        assert os.path.exists(path)
    finally:
        c.close()


def test_trigger_files_insert_queues_symbol_and_git(conn):
    _insert_file(conn, "/tmp/a.go", "hash1")
    assert _count(conn, "symbol_queue") == 1
    assert _count(conn, "git_queue") == 1


def test_trigger_files_update_only_on_hash_change(conn):
    fid = _insert_file(conn, "/tmp/a.go", "hash1")
    conn.execute("UPDATE files SET mtime=? WHERE id=?", (123, fid))
    assert _count(conn, "symbol_queue") == 1
    conn.execute("UPDATE files SET hash=? WHERE id=?", ("hash2", fid))
    assert _count(conn, "symbol_queue") == 2
    assert _count(conn, "git_queue") == 1


def test_trigger_symbols_insert_queues_description(conn):
    fid = _insert_file(conn, "/tmp/a.go", "hash1")
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid, "Foo", "function", 1, 5, "func Foo() {}"),
    )
    assert _count(conn, "description_queue") == 1
    assert _count(conn, "embedding_queue") == 1


def test_trigger_description_update_queues_embedding(conn):
    fid = _insert_file(conn, "/tmp/a.go", "hash1")
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid, "Foo", "function", 1, 5, ""),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE symbols SET description=? WHERE id=?", ("first", sid))
    assert _count(conn, "embedding_queue") == 2
    conn.execute("UPDATE symbols SET description=? WHERE id=?", ("second", sid))
    assert _count(conn, "embedding_queue") == 2


def test_cascade_delete_file(conn):
    fid = _insert_file(conn, "/tmp/a.go", "hash1")
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid, "Foo", "function", 1, 5, ""),
    )
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO embeddings(symbol_id, model, vector, embedded_at) VALUES(?,?,?,?)",
        (sid, "m", b"\x00\x00\x00\x00", 0),
    )
    conn.execute("DELETE FROM files WHERE id=?", (fid,))
    for tbl in ["symbols", "embeddings", "symbol_queue", "git_queue", "description_queue", "embedding_queue"]:
        assert _count(conn, tbl) == 0, tbl


def test_claim_batch_orders_by_queued_at(conn):
    id1 = _insert_file(conn, "/tmp/a.go", "h1")
    id2 = _insert_file(conn, "/tmp/b.go", "h2")
    id3 = _insert_file(conn, "/tmp/c.go", "h3")
    conn.execute("UPDATE symbol_queue SET queued_at=? WHERE file_id=?", (100, id1))
    conn.execute("UPDATE symbol_queue SET queued_at=? WHERE file_id=?", (200, id2))
    conn.execute("UPDATE symbol_queue SET queued_at=? WHERE file_id=?", (300, id3))

    rows = db.claim_batch(conn, "symbol_queue", "file_id", 2)
    assert len(rows) == 2
    assert rows[0].target_id == id1
    assert rows[1].target_id == id2


def test_claim_batch_marks_processing(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    _insert_file(conn, "/tmp/b.go", "h2")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 1)
    assert len(rows) == 1

    pending = int(conn.execute("SELECT COUNT(*) FROM symbol_queue WHERE status='pending'").fetchone()[0])
    processing = int(conn.execute("SELECT COUNT(*) FROM symbol_queue WHERE status='processing'").fetchone()[0])
    assert pending == 1
    assert processing == 1


def test_claim_batch_empty_returns_empty_list(conn):
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 5)
    assert rows == []


def test_mark_done_removes_row(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 1)
    db.mark_done(conn, "symbol_queue", rows[0].id)
    assert _count(conn, "symbol_queue") == 0


def test_requeue_below_cap_returns_false_and_stays_pending(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 1)

    before = int(time.time()) - 1
    failed = db.requeue(conn, "symbol_queue", rows[0].id, "transient", 3)
    assert failed is False

    status, attempts, err, queued_at, priority = conn.execute(
        "SELECT status, attempts, error, queued_at, priority FROM symbol_queue WHERE id=?",
        (rows[0].id,),
    ).fetchone()
    assert status == "pending"
    assert attempts == 1
    assert err == "transient"
    assert queued_at >= before
    assert priority == 0


def test_requeue_at_cap_returns_true_and_marks_failed(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 1)

    failed = db.requeue(conn, "symbol_queue", rows[0].id, "fatal", 1)
    assert failed is True

    status, attempts, err = conn.execute(
        "SELECT status, attempts, error FROM symbol_queue WHERE id=?",
        (rows[0].id,),
    ).fetchone()
    assert status == "failed"
    assert attempts == 1
    assert err == "fatal"


def test_requeue_failed_row_not_claimed_again(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 1)
    db.requeue(conn, "symbol_queue", rows[0].id, "dead", 1)
    claimed = db.claim_batch(conn, "symbol_queue", "file_id", 10)
    assert claimed == []


def test_recover_orphans_resets_processing(conn):
    _insert_file(conn, "/tmp/a.go", "h1")
    _insert_file(conn, "/tmp/b.go", "h2")
    rows = db.claim_batch(conn, "symbol_queue", "file_id", 2)
    assert len(rows) == 2

    processing = int(conn.execute("SELECT COUNT(*) FROM symbol_queue WHERE status='processing'").fetchone()[0])
    assert processing == 2

    db.recover_orphans(conn, "symbol_queue")
    pending = int(conn.execute("SELECT COUNT(*) FROM symbol_queue WHERE status='pending'").fetchone()[0])
    assert pending == 2


def test_claim_batch_priority_beats_age(conn):
    id_a = _insert_file(conn, "/tmp/a.go", "h1")
    id_b = _insert_file(conn, "/tmp/b.go", "h2")
    conn.execute("UPDATE symbol_queue SET queued_at=? WHERE file_id=?", (1, id_a))
    conn.execute("UPDATE symbol_queue SET queued_at=? WHERE file_id=?", (2, id_b))
    conn.execute("UPDATE symbol_queue SET priority=0 WHERE file_id=?", (id_a,))

    first = db.claim_batch(conn, "symbol_queue", "file_id", 1)
    assert len(first) == 1
    assert first[0].target_id == id_b


def test_format_eta_seconds():
    assert db.format_eta(0) == "0s"
    assert db.format_eta(1) == "1s"
    assert db.format_eta(59) == "59s"


def test_format_eta_minutes():
    assert db.format_eta(60) == "1m"
    assert db.format_eta(119) == "1m"
    assert db.format_eta(3599) == "59m"


def test_format_eta_hours():
    assert db.format_eta(3600) == "1h"
    assert db.format_eta(7200) == "2h"
    assert db.format_eta(86399) == "23h"


def test_format_eta_days():
    assert db.format_eta(86400) == "1d"
    assert db.format_eta(86400 * 3 + 500) == "3d"


def test_write_heartbeat_insert_and_update(conn):
    db.write_heartbeat(conn, db.Heartbeat("test-daemon", "running", 5, 10, 2, 1, 3.5, 120))
    row = conn.execute(
        "SELECT daemon, status, queue_depth, processed_today, retries_today, "
        "failures_today, rate_per_minute, eta_seconds, eta_display, last_error "
        "FROM daemon_status WHERE daemon=?",
        ("test-daemon",),
    ).fetchone()
    assert row[0] == "test-daemon"
    assert row[1] == "running"
    assert row[2] == 5
    assert row[3] == 10
    assert row[4] == 2
    assert row[5] == 1
    assert row[6] == 3.5
    assert row[7] == 120
    assert row[8] == "2m"
    assert row[9] is None

    db.write_heartbeat(conn, db.Heartbeat("test-daemon", "idle", 0, 11, 2, 1, 0.0, None, "boom"))
    row = conn.execute(
        "SELECT status, queue_depth, processed_today, eta_seconds, eta_display, last_error "
        "FROM daemon_status WHERE daemon=?",
        ("test-daemon",),
    ).fetchone()
    assert row[0] == "idle"
    assert row[1] == 0
    assert row[2] == 11
    assert row[3] is None
    assert row[4] is None
    assert row[5] == "boom"

    count = int(conn.execute("SELECT COUNT(*) FROM daemon_status WHERE daemon=?", ("test-daemon",)).fetchone()[0])
    assert count == 1


# ---------------------------------------------------------------------------
# get_or_create_rule tests
# ---------------------------------------------------------------------------

def test_get_or_create_rule_returns_id(conn):
    rid = db.get_or_create_rule(conn, "antislop", "confusing", "warning", "desc")
    assert isinstance(rid, int)
    assert rid > 0


def test_get_or_create_rule_is_idempotent(conn):
    rid1 = db.get_or_create_rule(conn, "antislop", "confusing", "warning", "desc")
    rid2 = db.get_or_create_rule(conn, "antislop", "confusing", "warning", "desc updated")
    assert rid1 == rid2


def test_get_or_create_rule_updates_description(conn):
    db.get_or_create_rule(conn, "antislop", "confusing", "warning", "original")
    db.get_or_create_rule(conn, "antislop", "confusing", "warning", "updated")
    row = conn.execute(
        "SELECT ar.description FROM analyzer_rules ar"
        " JOIN analyzers a ON a.id = ar.analyzer_id"
        " WHERE a.name='antislop' AND ar.name='confusing'"
    ).fetchone()
    assert row[0] == "updated"


def test_get_or_create_rule_different_rules_different_ids(conn):
    rid1 = db.get_or_create_rule(conn, "myanalyzer", "rule_a", "error", "desc a")
    rid2 = db.get_or_create_rule(conn, "myanalyzer", "rule_b", "warning", "desc b")
    assert rid1 != rid2


# ---------------------------------------------------------------------------
# ensure_analyzers tests
# ---------------------------------------------------------------------------

class _FakeRule:
    def __init__(self, name, severity, description):
        self.name = name
        self.severity = severity
        self.description = description


class _FakeAnalyzer:
    def __init__(self, name, description, rules):
        self.name = name
        self.description = description
        self.rules = rules


def test_ensure_analyzers_returns_mapping(conn):
    rules = [_FakeRule("r1", "error", "desc1"), _FakeRule("r2", "warning", "desc2")]
    analyzer = _FakeAnalyzer("myanalyzer", "My analyzer", rules)
    result = db.ensure_analyzers(conn, [analyzer])
    assert "myanalyzer" in result
    assert "r1" in result["myanalyzer"]
    assert "r2" in result["myanalyzer"]
    assert result["myanalyzer"]["r1"] != result["myanalyzer"]["r2"]


def test_ensure_analyzers_is_idempotent(conn):
    rules = [_FakeRule("r1", "error", "desc1")]
    analyzer = _FakeAnalyzer("myanalyzer", "desc", rules)
    result1 = db.ensure_analyzers(conn, [analyzer])
    result2 = db.ensure_analyzers(conn, [analyzer])
    assert result1 == result2


def test_ensure_analyzers_updates_rule_description(conn):
    rules_v1 = [_FakeRule("r1", "error", "original")]
    rules_v2 = [_FakeRule("r1", "error", "updated")]
    db.ensure_analyzers(conn, [_FakeAnalyzer("a", "", rules_v1)])
    db.ensure_analyzers(conn, [_FakeAnalyzer("a", "", rules_v2)])
    row = conn.execute(
        "SELECT ar.description FROM analyzer_rules ar"
        " JOIN analyzers a ON a.id = ar.analyzer_id"
        " WHERE a.name='a' AND ar.name='r1'"
    ).fetchone()
    assert row[0] == "updated"


def test_ensure_analyzers_orphan_rules_stay(conn):
    rules = [_FakeRule("r1", "error", "desc"), _FakeRule("r2", "warning", "desc")]
    db.ensure_analyzers(conn, [_FakeAnalyzer("a", "", rules)])
    # Second run with only r1
    db.ensure_analyzers(conn, [_FakeAnalyzer("a", "", [_FakeRule("r1", "error", "desc")])])
    count = conn.execute(
        "SELECT COUNT(*) FROM analyzer_rules ar"
        " JOIN analyzers a ON a.id = ar.analyzer_id WHERE a.name='a'"
    ).fetchone()[0]
    assert count == 2
