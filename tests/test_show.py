from __future__ import annotations

from pathlib import Path

import pytest

import blerk_cmd.show as show_mod
from blerk import db


@pytest.fixture
def sample_file(tmp_path):
    src = tmp_path / "src" / "demo.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "def foo():\n"
        "    x = 1\n"
        "    return x\n"
        "\n"
        "class Bar:\n"
        "    def method(self):\n"
        "        return 2\n"
    )
    return src


@pytest.fixture
def conn(tmp_path, sample_file):
    db_path = tmp_path / "index.db"
    conn = db.open_db(str(db_path))
    conn.execute(
        "INSERT INTO files(path, mtime, size, hash) VALUES (?, ?, ?, ?)",
        (str(sample_file).replace("\\", "/"), 1, sample_file.stat().st_size, "abc"),
    )
    file_id = conn.execute("SELECT id FROM files WHERE path=?", (str(sample_file).replace("\\", "/"),)).fetchone()[0]
    conn.executemany(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, params) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (file_id, "foo", "function", 1, 3, ""),
            (file_id, "Bar", "class", 5, 7, ""),
            (file_id, "method", "method", 6, 7, "self"),
        ],
    )
    return conn


class TestReadLines:
    def test_basic_line_numbering(self, sample_file):
        text = show_mod._read_lines(str(sample_file), start=1, end=3)
        assert "demo.py" in text
        assert "1  def foo():" in text
        assert "2      x = 1" in text
        assert "3      return x" in text

    def test_symbol_range_only(self, sample_file):
        text = show_mod._read_lines(str(sample_file), start=5, end=7)
        assert "class Bar:" in text
        assert "def method" in text
        assert "return 2" in text

    def test_max_lines_truncates(self, sample_file):
        text = show_mod._read_lines(str(sample_file), start=1, end=7, max_lines=3)
        assert "[truncated" in text
        assert text.count("\n") == 3  # header + 2 newlines separating 3 lines

    def test_file_not_found(self):
        text = show_mod._read_lines("/nonexistent/path.py")
        assert "File not found" in text


class TestShowBySymbol:
    def test_finds_symbol_by_name(self, conn):
        result = show_mod.show(conn, "foo")
        assert "demo.py" in result
        assert "def foo():" in result
        assert "1  def foo():" in result

    def test_symbol_with_path_filter(self, conn):
        result = show_mod.show(conn, "method", path_filter="demo.py")
        assert "def method" in result

    def test_no_match(self, conn):
        result = show_mod.show(conn, "nonexistent")
        assert "No indexed file or symbol" in result


class TestShowByFile:
    def test_finds_file_by_name(self, conn):
        result = show_mod.show(conn, "demo.py")
        assert "def foo():" in result

    def test_finds_file_by_relative_path(self, conn, sample_file):
        result = show_mod.show(conn, str(sample_file))
        assert "class Bar" in result


class TestMainCli:
    def test_cli_invocation(self, capsys, tmp_path, sample_file):
        config_path = tmp_path / "blerk.toml"
        db_path = str(tmp_path / "index.db").replace("\\", "/")
        config_path.write_text(
            f'[db]\npath = "{db_path}"\n'
            '[watch]\nfolders = []\n'
        )

        # Populate DB so command can find symbol 'foo'.
        conn = db.open_db(str(tmp_path / "index.db"))
        conn.execute(
            "INSERT INTO files(path, mtime, size, hash) VALUES (?, ?, ?, ?)",
            (str(sample_file).replace("\\", "/"), 1, sample_file.stat().st_size, "abc"),
        )
        file_id = conn.execute("SELECT id FROM files WHERE path=?", (str(sample_file).replace("\\", "/"),)).fetchone()[0]
        conn.execute(
            "INSERT INTO symbols(file_id, name, kind, line, end_line, params) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, "foo", "function", 1, 3, ""),
        )
        conn.close()

        code = show_mod.main(["--config", str(config_path), "foo"])
        captured = capsys.readouterr()
        assert code == 0
        assert "def foo():" in captured.out
