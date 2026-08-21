from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from blerk import db
from blerk_cmd import watch_folder as wf



def test_debouncer_only_last_event_fires():
    received: list[dict[str, str]] = []
    done = threading.Event()

    def flush(events: dict[str, str]) -> None:
        received.append(events)
        done.set()

    d = wf.Debouncer(0.05, flush)
    for i in range(20):
        d.add("/tmp/a", f"event{i}")
        time.sleep(0.005)

    assert done.wait(timeout=1.0)
    time.sleep(0.1)

    assert len(received) == 1
    assert received[0] == {"/tmp/a": "event19"}


def test_debouncer_multiple_paths_accumulate():
    received: list[dict[str, str]] = []
    done = threading.Event()

    def flush(events: dict[str, str]) -> None:
        received.append(events)
        done.set()

    d = wf.Debouncer(0.05, flush)
    d.add("/tmp/a", "create")
    d.add("/tmp/b", "modify")
    d.add("/tmp/a", "modify")

    assert done.wait(timeout=1.0)
    assert len(received) == 1
    assert received[0] == {"/tmp/a": "modify", "/tmp/b": "modify"}


def test_upsert_skips_when_hash_unchanged(conn, tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    path = str(f)

    wf.upsert_file(conn, path)
    row1 = conn.execute(
        "SELECT fp.mtime, f.size, f.hash FROM file_paths fp JOIN files f ON f.id=fp.file_id WHERE fp.path=?",
        (path.replace("\\", "/"),),
    ).fetchone()
    assert row1 is not None

    before = wf._upsert_count.load()
    wf.upsert_file(conn, path)
    after = wf._upsert_count.load()

    assert after == before

    row2 = conn.execute(
        "SELECT fp.mtime, f.size, f.hash FROM file_paths fp JOIN files f ON f.id=fp.file_id WHERE fp.path=?",
        (path.replace("\\", "/"),),
    ).fetchone()
    assert row1 == row2

    count = int(conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0])
    assert count == 1


def test_upsert_is_idempotent(conn, tmp_path):
    """Calling upsert_file twice on an unchanged file does not increment the upsert counter."""
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    path = str(f).replace("\\", "/")

    wf.upsert_file(conn, path)
    before = wf._upsert_count.load()
    wf.upsert_file(conn, path)
    after = wf._upsert_count.load()

    assert after == before
    assert int(conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0]) == 1


def test_upsert_updates_when_content_changes(conn, tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    path = str(f)

    wf.upsert_file(conn, path)
    row1 = conn.execute(
        "SELECT fp.mtime, f.size, f.hash FROM file_paths fp JOIN files f ON f.id=fp.file_id WHERE fp.path=?",
        (path.replace("\\", "/"),),
    ).fetchone()

    time.sleep(1.1)
    f.write_text("hello world 2", encoding="utf-8")

    wf.upsert_file(conn, path)
    row2 = conn.execute(
        "SELECT fp.mtime, f.size, f.hash FROM file_paths fp JOIN files f ON f.id=fp.file_id WHERE fp.path=?",
        (path.replace("\\", "/"),),
    ).fetchone()

    assert row1[2] != row2[2]
    assert row1[1] != row2[1]


def test_scan_walks_recursively_and_upserts(conn, tmp_path):
    scan_root = tmp_path / "repo"
    scan_root.mkdir()
    (scan_root / "a.py").write_text("print('a')", encoding="utf-8")
    (scan_root / "sub").mkdir()
    (scan_root / "sub" / "b.py").write_text("print('b')", encoding="utf-8")
    (scan_root / "sub" / "nested").mkdir()
    (scan_root / "sub" / "nested" / "c.py").write_text("print('c')", encoding="utf-8")

    all_sets: list = []
    wf._scan_dir(str(scan_root), [], conn, all_sets)

    count = int(conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0])
    assert count == 3

    paths = {r[0] for r in conn.execute("SELECT path FROM file_paths").fetchall()}
    expected = {
        str(scan_root / "a.py").replace("\\", "/"),
        str(scan_root / "sub" / "b.py").replace("\\", "/"),
        str(scan_root / "sub" / "nested" / "c.py").replace("\\", "/"),
    }
    assert paths == expected


def test_scan_respects_gitignore(conn, tmp_path):
    scan_root = tmp_path / "repo"
    scan_root.mkdir()
    (scan_root / "keep.py").write_text("keep", encoding="utf-8")
    (scan_root / "skip.log").write_text("skip", encoding="utf-8")
    (scan_root / ".gitignore").write_text("*.log\n", encoding="utf-8")

    wf._scan_dir(str(scan_root), [], conn, [])

    paths = {r[0] for r in conn.execute("SELECT path FROM file_paths").fetchall()}
    assert str(scan_root / "keep.py").replace("\\", "/") in paths
    assert str(scan_root / "skip.log").replace("\\", "/") not in paths


def test_scan_skips_dir_only_ignored_dirs(conn, tmp_path):
    scan_root = tmp_path / "repo"
    scan_root.mkdir()
    (scan_root / "src").mkdir()
    (scan_root / "src" / "main.py").write_text("m", encoding="utf-8")
    (scan_root / "build").mkdir()
    (scan_root / "build" / "out.bin").write_text("b", encoding="utf-8")
    (scan_root / ".gitignore").write_text("build/\n", encoding="utf-8")

    wf._scan_dir(str(scan_root), [], conn, [])

    paths = {r[0] for r in conn.execute("SELECT path FROM file_paths").fetchall()}
    assert str(scan_root / "src" / "main.py").replace("\\", "/") in paths
    assert str(scan_root / "build" / "out.bin").replace("\\", "/") not in paths


def test_delete_file_removes_row(conn, tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("bye", encoding="utf-8")
    wf.upsert_file(conn, str(f))
    assert int(conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0]) == 1
    wf.delete_file(conn, str(f))
    assert int(conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0]) == 0


def test_hash_file_matches_sha1(tmp_path):
    import hashlib
    f = tmp_path / "x"
    data = b"blerk\n" * 1000
    f.write_bytes(data)
    assert wf.hash_file(str(f)) == hashlib.sha1(data).hexdigest()


def test_debouncer_drain_returns_pending_events():
    received: list[dict[str, str]] = []

    def flush(events: dict[str, str]) -> None:
        received.append(events)

    d = wf.Debouncer(60.0, flush)
    d.add("/tmp/a", "modify")
    d.add("/tmp/b", "remove")

    events = d.drain()
    assert events == {"/tmp/a": "modify", "/tmp/b": "remove"}
    assert d.drain() == {}

    # Give any stray timer threads a moment; no flush should have been invoked.
    time.sleep(0.05)
    assert received == []
