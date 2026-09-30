from __future__ import annotations

import pytest

from blerk import db
from blerk_cmd import status as status_mod
from blerk_cmd.status import status


def _make_db(tmp_path):
    db_path = str(tmp_path / "blerk.db")
    conn = db.open_db(db_path)
    return conn, db_path


class TestHubRow:
    """The daemons are threads of the hub now, so status reports the hub rather than a UDP coordinator."""

    def test_running_shows_pid(self, tmp_path, monkeypatch):
        conn, db_path = _make_db(tmp_path)
        monkeypatch.setattr(status_mod, "running_hub_pid", lambda: 4242)
        result = status(conn, db_path)
        assert "hub" in result
        assert "running" in result
        assert "4242" in result

    def test_not_running_when_no_hub(self, tmp_path, monkeypatch):
        conn, db_path = _make_db(tmp_path)
        monkeypatch.setattr(status_mod, "running_hub_pid", lambda: None)
        result = status(conn, db_path)
        assert "hub" in result
        assert "not running" in result

    def test_no_db_path_omits_hub_row(self, tmp_path, monkeypatch):
        conn, db_path = _make_db(tmp_path)
        monkeypatch.setattr(status_mod, "running_hub_pid", lambda: 4242)
        result = status(conn)
        assert "hub " not in result

    def test_hub_row_appears_first(self, tmp_path, monkeypatch):
        conn, db_path = _make_db(tmp_path)
        monkeypatch.setattr(status_mod, "running_hub_pid", lambda: 9999)
        result = status(conn, db_path)
        lines = [l for l in result.splitlines() if l.strip()]
        assert "hub" in lines[0]
