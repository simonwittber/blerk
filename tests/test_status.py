from __future__ import annotations

import pytest

from blerk import db
from blerk.coordinator import _port_file, _workers_dir
from blerk_cmd.status import status


def _make_db(tmp_path):
    db_path = str(tmp_path / "blerk.db")
    conn = db.open_db(db_path)
    return conn, db_path


class TestCoordinatorRow:
    def test_running_shows_port(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        _port_file(db_path).write_text("54321")
        result = status(conn, db_path)
        assert "coordinator" in result
        assert "running" in result
        assert "54321" in result

    def test_not_running_when_no_port_file(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        result = status(conn, db_path)
        assert "coordinator" in result
        assert "not running" in result

    def test_worker_count_shown(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        _port_file(db_path).write_text("12345")
        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "q-1.worker").write_text("pid=1\nport=100\nqueue=q\n")
        (wd / "q-2.worker").write_text("pid=2\nport=101\nqueue=q\n")
        result = status(conn, db_path)
        assert "2 workers" in result

    def test_zero_workers_shown(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        _port_file(db_path).write_text("12345")
        result = status(conn, db_path)
        assert "0 workers" in result

    def test_no_db_path_omits_coordinator_row(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        _port_file(db_path).write_text("54321")
        result = status(conn)
        assert "coordinator" not in result

    def test_coordinator_row_appears_first(self, tmp_path):
        conn, db_path = _make_db(tmp_path)
        _port_file(db_path).write_text("9999")
        result = status(conn, db_path)
        lines = [l for l in result.splitlines() if l.strip()]
        assert "coordinator" in lines[0]
