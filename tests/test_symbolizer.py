from __future__ import annotations

import pytest

from blerk import config, db
from blerk.symbols.types import CallRef, Symbol

from blerk_cmd.symbolizer import process_symbols



def _cfg(min_lines: int = 0) -> config.Config:
    cfg = config.defaults()
    cfg.symbolizer.min_describe_lines = min_lines
    return cfg


def _insert_file(conn, path: str = "a/b.py") -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (path, 0, "h"),
    )
    return int(cur.lastrowid)


def test_process_symbols_inserts_symbols_and_refs(conn):
    cfg = _cfg(min_lines=0)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)
    syms = [
        Symbol(name="foo", kind="function", line=1, end_line=10, snippet="def foo():..."),
        Symbol(name="bar", kind="function", line=20, end_line=30, snippet="def bar():..."),
    ]
    refs = [CallRef(caller_name="foo", callee_name="bar")]

    process_symbols(conn, cfg, row, "a/b.py", syms, refs)

    names = [r[0] for r in conn.execute(
        "SELECT name FROM symbols WHERE file_id=? ORDER BY line", (fid,)
    ).fetchall()]
    assert names == ["foo", "bar"]

    ref_count = int(conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0])
    assert ref_count == 1

    caller_id, callee_id = conn.execute(
        "SELECT caller_id, callee_id FROM symbol_refs"
    ).fetchone()
    caller_name = conn.execute(
        "SELECT name FROM symbols WHERE id=?", (caller_id,)
    ).fetchone()[0]
    callee_name = conn.execute(
        "SELECT name FROM symbols WHERE id=?", (callee_id,)
    ).fetchone()[0]
    assert caller_name == "foo"
    assert callee_name == "bar"


def test_process_symbols_rerun_replaces_old_rows(conn):
    cfg = _cfg(min_lines=0)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)

    process_symbols(
        conn, cfg, row, "a/b.py",
        [Symbol("foo", "function", 1, 10, "s1")],
        [],
    )
    process_symbols(
        conn, cfg, row, "a/b.py",
        [Symbol("baz", "function", 1, 10, "s2")],
        [],
    )

    names = [r[0] for r in conn.execute(
        "SELECT name FROM symbols WHERE file_id=?", (fid,)
    ).fetchall()]
    assert names == ["baz"]


def test_unchanged_snippet_carries_description_forward(conn):
    cfg = _cfg(min_lines=0)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)
    syms = [Symbol(name="foo", kind="function", line=1, end_line=10, snippet="def foo(): pass")]

    process_symbols(conn, cfg, row, "a/b.py", syms, [])

    sym_id = conn.execute("SELECT id FROM symbols WHERE file_id=?", (fid,)).fetchone()[0]
    conn.execute("UPDATE symbols SET description='old desc', described_at=999 WHERE id=?", (sym_id,))
    conn.execute("DELETE FROM description_queue WHERE symbol_id=?", (sym_id,))

    # Re-symbolize with identical snippet.
    process_symbols(conn, cfg, row, "a/b.py", syms, [])

    new_sym = conn.execute(
        "SELECT description, described_at FROM symbols WHERE file_id=?", (fid,)
    ).fetchone()
    assert new_sym[0] == "old desc"
    assert new_sym[1] == 999

    queue_count = conn.execute(
        "SELECT COUNT(*) FROM description_queue dq "
        "JOIN symbols s ON s.id = dq.symbol_id WHERE s.file_id=?",
        (fid,),
    ).fetchone()[0]
    assert queue_count == 0


def test_changed_snippet_requeues_for_description(conn):
    cfg = _cfg(min_lines=0)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)
    syms_v1 = [Symbol(name="foo", kind="function", line=1, end_line=10, snippet="def foo(): pass")]
    syms_v2 = [Symbol(name="foo", kind="function", line=1, end_line=10, snippet="def foo(): return 1")]

    process_symbols(conn, cfg, row, "a/b.py", syms_v1, [])

    sym_id = conn.execute("SELECT id FROM symbols WHERE file_id=?", (fid,)).fetchone()[0]
    conn.execute("UPDATE symbols SET description='old desc' WHERE id=?", (sym_id,))
    conn.execute("DELETE FROM description_queue WHERE symbol_id=?", (sym_id,))

    # Re-symbolize with changed snippet.
    process_symbols(conn, cfg, row, "a/b.py", syms_v2, [])

    new_sym = conn.execute(
        "SELECT description FROM symbols WHERE file_id=?", (fid,)
    ).fetchone()
    assert new_sym[0] is None

    queue_count = conn.execute(
        "SELECT COUNT(*) FROM description_queue dq "
        "JOIN symbols s ON s.id = dq.symbol_id WHERE s.file_id=?",
        (fid,),
    ).fetchone()[0]
    assert queue_count == 1


def test_new_symbol_queues_while_described_unchanged_does_not(conn):
    cfg = _cfg(min_lines=0)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)

    process_symbols(conn, cfg, row, "a/b.py",
                    [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass")], [])

    # Simulate describer completing for foo.
    sym_id = conn.execute("SELECT id FROM symbols WHERE file_id=?", (fid,)).fetchone()[0]
    conn.execute("UPDATE symbols SET description='described', described_at=1 WHERE id=?", (sym_id,))
    conn.execute("DELETE FROM description_queue WHERE symbol_id=?", (sym_id,))

    # Re-symbolize: foo unchanged, bar is new.
    process_symbols(conn, cfg, row, "a/b.py",
                    [Symbol(name="foo", kind="function", line=1, end_line=5, snippet="def foo(): pass"),
                     Symbol(name="bar", kind="function", line=10, end_line=15, snippet="def bar(): pass")], [])

    queued = [r[0] for r in conn.execute(
        "SELECT s.name FROM description_queue dq "
        "JOIN symbols s ON s.id = dq.symbol_id WHERE s.file_id=?",
        (fid,),
    ).fetchall()]
    assert "bar" in queued
    assert "foo" not in queued


def test_min_describe_lines_removes_short_symbols(conn):
    cfg = _cfg(min_lines=5)
    fid = _insert_file(conn)
    row = db.QueueRow(id=1, target_id=fid)
    syms = [
        Symbol(name="short_fn", kind="function", line=1, end_line=3, snippet="def s():..."),
        Symbol(name="long_fn", kind="function", line=10, end_line=30, snippet="def l():..."),
    ]

    process_symbols(conn, cfg, row, "a/b.py", syms, [])

    remaining = conn.execute(
        "SELECT s.name FROM description_queue dq "
        "JOIN symbols s ON s.id = dq.symbol_id "
        "WHERE s.file_id=?",
        (fid,),
    ).fetchall()
    remaining_names = sorted(r[0] for r in remaining)
    assert remaining_names == ["long_fn"]
