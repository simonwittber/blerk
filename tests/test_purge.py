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
    purge_orphans,
    purge_worktrees,
)


def _insert_file(conn: sqlite3.Connection, path: str) -> int:
    conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", (path,))
    fid = int(conn.execute("SELECT id FROM files WHERE hash=?", (path,)).fetchone()[0])
    conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (path, fid))
    conn.commit()
    return fid


def _file_exists(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM file_paths WHERE path=?", (path,)).fetchone() is not None


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


# --- purge_orphans ---


def _seed_indexed(conn: sqlite3.Connection, path: str | None, content_hash: str) -> int:
    """Insert a file with one symbol, one code block and one embedding.

    Pass path=None to create content with no file_paths row, which is what a database written before
    the file_paths_after_delete_orphan trigger existed still contains.
    """
    if path is None:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", (content_hash,))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", (content_hash,)).fetchone()[0])
    else:
        fid = _insert_file(conn, path)
    sid = int(conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (fid, f"sym_{content_hash}", "function", 1, 5),
    ).lastrowid)
    conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, content_hash, start_line, end_line)"
        " VALUES(?,?,?,?,?,?)",
        (sid, 0, "body", content_hash, 1, 5),
    )
    conn.execute(
        "INSERT INTO embeddings(content_hash, model, vector, embedded_at)"
        " VALUES(?,?,?,unixepoch())",
        (content_hash, "m", b"\x00" * 16),
    )
    conn.commit()
    return fid


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    one = lambda s: conn.execute(s).fetchone()[0]
    return (one("SELECT COUNT(*) FROM files"), one("SELECT COUNT(*) FROM symbols"),
            one("SELECT COUNT(*) FROM code_blocks"), one("SELECT COUNT(*) FROM embeddings"))


def test_dropping_a_path_already_removes_its_content(conn):
    """The file_paths_after_delete_orphan trigger keeps new deletions clean, so the sweep is a repair tool."""
    _seed_indexed(conn, "/repo/gone.py", "hgone")
    conn.execute("DELETE FROM file_paths WHERE path=?", ("/repo/gone.py",))
    conn.commit()

    assert _counts(conn)[:3] == (0, 0, 0)
    # The trigger cascades through files, but embeddings has no foreign key and survives.
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


def test_purge_orphans_cascades_to_symbols_blocks_and_embeddings(conn):
    _seed_indexed(conn, "/repo/keep.py", "hkeep")
    _seed_indexed(conn, None, "hgone")
    assert _counts(conn) == (2, 2, 2, 2)

    n_files, n_emb = purge_orphans(conn)

    assert (n_files, n_emb) == (1, 1)
    assert _counts(conn) == (1, 1, 1, 1)
    assert conn.execute("SELECT name FROM symbols").fetchone()[0] == "sym_hkeep"
    assert conn.execute("SELECT content_hash FROM embeddings").fetchone()[0] == "hkeep"


def test_purge_orphans_keeps_content_shared_by_another_path(conn):
    """Identical content at two paths shares one files row, so removing one path must keep it."""
    fid = _seed_indexed(conn, "/repo/a.py", "shared")
    conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("/repo/b.py", fid))
    conn.execute("DELETE FROM file_paths WHERE path=?", ("/repo/a.py",))
    conn.commit()

    assert purge_orphans(conn) == (0, 0)
    assert _counts(conn) == (1, 1, 1, 1)


def test_purge_orphans_dry_run_predicts_the_real_run(conn):
    _seed_indexed(conn, "/repo/keep.py", "hkeep")
    _seed_indexed(conn, None, "hgone")

    predicted = purge_orphans(conn, dry_run=True)
    assert _counts(conn) == (2, 2, 2, 2), "dry run must change nothing"
    assert predicted == purge_orphans(conn), "dry run must match what the real run removes"


def test_purge_orphans_removes_embedding_whose_block_is_gone(conn):
    _seed_indexed(conn, "/repo/keep.py", "hkeep")
    conn.execute(
        "INSERT INTO embeddings(content_hash, model, vector, embedded_at)"
        " VALUES(?,?,?,unixepoch())",
        ("never_indexed", "m", b"\x00" * 16),
    )
    conn.commit()

    n_files, n_emb = purge_orphans(conn)

    assert (n_files, n_emb) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


def test_purge_orphans_noop_on_clean_index(conn):
    _seed_indexed(conn, "/repo/a.py", "ha")
    assert purge_orphans(conn) == (0, 0)
    assert _counts(conn) == (1, 1, 1, 1)
