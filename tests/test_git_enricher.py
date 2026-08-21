from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from blerk import config, db

from blerk_cmd.git_enricher import find_git_root, parse_branch, process_row


def test_find_git_root_finds_parent(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "sub"
    nested.mkdir(parents=True)

    result = find_git_root(str(nested))
    assert result == str(repo.resolve()).replace("\\", "/")


def test_find_git_root_returns_none_outside_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = find_git_root(str(plain))
    assert result is None


def test_parse_branch_head_arrow():
    assert parse_branch("HEAD -> main, origin/main") == "main"


def test_parse_branch_feature_after_tag():
    assert parse_branch("HEAD, tag: v1.0, feature/x") == "tag: v1.0"


def test_parse_branch_skips_head_and_origin():
    assert parse_branch("HEAD, origin/main, feature/x") == "feature/x"


def test_parse_branch_only_origin():
    assert parse_branch("HEAD, origin/main") == ""


def test_parse_branch_empty():
    assert parse_branch("") == ""


def _have_git() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


@pytest.mark.skipif(not _have_git(), reason="git not available on PATH")
def test_process_row_enriches_file_from_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=env,
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test Author")
    git("config", "user.email", "test@example.com")

    file_path = repo / "hello.txt"
    file_path.write_text("hello\n")
    git("add", "hello.txt")
    git("commit", "-m", "initial")

    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES('h', 0)")
        fid = int(conn.execute("SELECT id FROM files WHERE hash='h'").fetchone()[0])
        conn.execute(
            "INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)",
            (str(file_path), fid),
        )
        fp_id = int(conn.execute(
            "SELECT id FROM file_paths WHERE path=?", (str(file_path),)
        ).fetchone()[0])

        queue_row = conn.execute(
            "SELECT id FROM git_queue WHERE file_path_id=?", (fp_id,)
        ).fetchone()
        assert queue_row is not None
        queue_id = int(queue_row[0])

        rows = db.claim_batch(conn, "git_queue", "file_path_id", 10)
        row = next(r for r in rows if r.id == queue_id)

        cfg = config.defaults()
        retried, failed = process_row(conn, cfg, row)
        assert not retried
        assert not failed

        result = conn.execute(
            "SELECT git_commit, git_author, git_branch, git_enriched_at "
            "FROM git_files WHERE file_id=?",
            (fid,),
        ).fetchone()
        assert result is not None, "no git_files row created"
        commit, author, branch, enriched_at = result
        assert commit and len(commit) == 40
        assert author == "Test Author"
        assert branch == "main"
        assert enriched_at is not None and enriched_at > 0

        remaining = conn.execute(
            "SELECT COUNT(*) FROM git_queue WHERE id=?", (queue_id,)
        ).fetchone()[0]
        assert remaining == 0
    finally:
        conn.close()
