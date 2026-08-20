from __future__ import annotations

import os
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from blerk_cmd.purge import (
    _collect_ignore_sets,
    _worktree_paths,
    purge_gitignored,
    purge_missing,
    purge_worktrees,
)


def _insert_file(conn: sqlite3.Connection, path: str) -> int:
    conn.execute(
        "INSERT INTO files(path, mtime, size, hash) VALUES (?, 0, 0, 'abc')",
        (path,),
    )
    conn.commit()
    return conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()[0]


def _file_exists(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone() is not None


# --- purge_missing ---


def test_purge_missing_removes_nonexistent(conn, tmp_path):
    real = tmp_path / "exists.py"
    real.write_text("x")
    _insert_file(conn, str(real))
    _insert_file(conn, "/no/such/file.py")

    n = purge_missing(conn)

    assert n == 1
    assert _file_exists(conn, str(real))
    assert not _file_exists(conn, "/no/such/file.py")


def test_purge_missing_dry_run_does_not_delete(conn):
    _insert_file(conn, "/ghost/file.py")

    n = purge_missing(conn, dry_run=True)

    assert n == 1
    assert _file_exists(conn, "/ghost/file.py")


def test_purge_missing_nothing_to_do(conn, tmp_path):
    real = tmp_path / "here.py"
    real.write_text("x")
    _insert_file(conn, str(real))

    assert purge_missing(conn) == 0


# --- purge_worktrees ---


_PORCELAIN = """\
worktree /main/repo
HEAD abc123
branch refs/heads/main

worktree /worktrees/feature
HEAD def456
branch refs/heads/feature

worktree /worktrees/hotfix
HEAD 789abc
branch refs/heads/hotfix
"""


def test_worktree_paths_skips_main():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_PORCELAIN
        )
        paths = _worktree_paths("/main/repo")

    assert os.path.normpath("/worktrees/feature") in paths
    assert os.path.normpath("/worktrees/hotfix") in paths
    assert os.path.normpath("/main/repo") not in paths


def test_purge_worktrees_removes_files_under_worktree(conn):
    wt = os.path.normpath("/worktrees/feature")
    _insert_file(conn, os.path.join(wt, "foo.py"))
    _insert_file(conn, os.path.join(wt, "sub", "bar.py"))
    _insert_file(conn, os.path.normpath("/main/repo/main.py"))

    with patch("blerk_cmd.purge._worktree_paths", return_value=[wt]):
        n = purge_worktrees(conn, ["/main/repo"])

    assert n == 2
    assert _file_exists(conn, os.path.normpath("/main/repo/main.py"))
    assert not _file_exists(conn, os.path.join(wt, "foo.py"))


def test_purge_worktrees_dry_run(conn):
    wt = os.path.normpath("/worktrees/feature")
    _insert_file(conn, os.path.join(wt, "foo.py"))

    with patch("blerk_cmd.purge._worktree_paths", return_value=[wt]):
        n = purge_worktrees(conn, ["/main/repo"], dry_run=True)

    assert n == 1
    assert _file_exists(conn, os.path.join(wt, "foo.py"))


def test_purge_worktrees_no_worktrees(conn):
    _insert_file(conn, "/main/repo/foo.py")

    with patch("blerk_cmd.purge._worktree_paths", return_value=[]):
        n = purge_worktrees(conn, ["/main/repo"])

    assert n == 0


# --- _collect_ignore_sets / purge_gitignored ---


def test_collect_ignore_sets_reads_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("*.tmp\n")

    sets = _collect_ignore_sets(str(tmp_path))

    assert len(sets) == 2
    dirs = {s.dir for s in sets}
    assert str(tmp_path) in dirs
    assert str(sub) in dirs


def test_collect_ignore_sets_skips_worktree_dirs(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../.git/worktrees/wt\n")
    (wt / ".gitignore").write_text("*.tmp\n")

    sets = _collect_ignore_sets(str(tmp_path))

    dirs = {s.dir for s in sets}
    assert str(wt) not in dirs


def test_purge_gitignored_removes_matching_files(conn, tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    _insert_file(conn, str(tmp_path / "app.log"))
    _insert_file(conn, str(tmp_path / "main.py"))

    n = purge_gitignored(conn, [str(tmp_path)])

    assert n == 1
    assert _file_exists(conn, str(tmp_path / "main.py"))
    assert not _file_exists(conn, str(tmp_path / "app.log"))


def test_purge_gitignored_dry_run(conn, tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    _insert_file(conn, str(tmp_path / "app.log"))

    n = purge_gitignored(conn, [str(tmp_path)], dry_run=True)

    assert n == 1
    assert _file_exists(conn, str(tmp_path / "app.log"))


def test_purge_gitignored_no_sets(conn):
    _insert_file(conn, "/some/file.py")
    assert purge_gitignored(conn, []) == 0
