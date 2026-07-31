from __future__ import annotations

import pytest

from blerk import db
from blerk_cmd.hub import _purge_folder


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    conn.close()
    return path


def _insert_file(db_path: str, path: str) -> int:
    conn = db.open_db(db_path)
    cur = conn.execute(
        "INSERT INTO files(path, mtime, size, hash) VALUES(?,?,?,?)",
        (path, 0, 0, "x"),
    )
    fid = int(cur.lastrowid)
    conn.close()
    return fid


def _count(db_path: str, table: str) -> int:
    conn = db.open_db(db_path)
    n = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    conn.close()
    return n


def test_purge_folder_removes_matching_files(db_path):
    _insert_file(db_path, "/project/src/a.py")
    _insert_file(db_path, "/project/src/b.py")
    _insert_file(db_path, "/other/c.py")

    _purge_folder(db_path, "/project/src")

    conn = db.open_db(db_path)
    remaining = [r[0] for r in conn.execute("SELECT path FROM files").fetchall()]
    conn.close()
    assert remaining == ["/other/c.py"]


def test_purge_folder_cascades_to_queues(db_path):
    _insert_file(db_path, "/project/src/a.py")

    _purge_folder(db_path, "/project/src")

    for table in ("symbol_queue", "git_queue"):
        assert _count(db_path, table) == 0, table


def test_purge_folder_leaves_unrelated_queue_entries(db_path):
    _insert_file(db_path, "/project/src/a.py")
    _insert_file(db_path, "/other/b.py")

    _purge_folder(db_path, "/project/src")

    assert _count(db_path, "symbol_queue") == 1
    assert _count(db_path, "git_queue") == 1


def test_purge_folder_trailing_slash_normalised(db_path):
    _insert_file(db_path, "/project/src/a.py")

    _purge_folder(db_path, "/project/src/")

    assert _count(db_path, "files") == 0


def test_purge_folder_backslash_normalised(db_path):
    _insert_file(db_path, "/project/src/a.py")

    _purge_folder(db_path, "\\project\\src")

    assert _count(db_path, "files") == 0


def test_purge_folder_no_prefix_match_leaves_all(db_path):
    _insert_file(db_path, "/project/src/a.py")

    _purge_folder(db_path, "/project/other")

    assert _count(db_path, "files") == 1


def test_purge_folder_bad_db_path_does_not_raise():
    _purge_folder("/nonexistent/path/test.db", "/some/folder")
