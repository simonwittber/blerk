from __future__ import annotations

import pytest

from blerk import db


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = db.open_db(path)
    yield c
    c.close()


@pytest.fixture
def write_temp(tmp_path):
    def _write(name: str, src: str) -> str:
        p = tmp_path / name
        p.write_bytes(src.encode("utf-8"))
        return str(p)
    return _write
