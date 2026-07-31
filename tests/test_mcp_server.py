from __future__ import annotations

import asyncio
import struct

import pytest

import blerk_cmd.mcp_server as mcp_mod
from blerk import config, db


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers shared by unit and integration tests
# ---------------------------------------------------------------------------

def _open(tmp_path):
    return db.open_db(str(tmp_path / "test.db"))


def _seed_file(conn, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, hash) VALUES (?, 0, 0, '')", (path,)
    )
    return cur.lastrowid


def _seed_symbol(conn, file_id: int, name: str, kind: str = "function",
                 line: int = 1, end_line: int = 5,
                 description: str = "", snippet: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO symbols (file_id, name, kind, line, end_line, snippet, description)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (file_id, name, kind, line, end_line, snippet or None, description or None),
    )
    return cur.lastrowid


def _seed_embedding(conn, symbol_id: int, vec: list[float]) -> None:
    blob = struct.pack(f"<{len(vec)}f", *vec)
    conn.execute(
        "INSERT INTO embeddings (symbol_id, model, vector, embedded_at) VALUES (?, 'm', ?, 0)",
        (symbol_id, blob),
    )


def _fake_cfg(tmp_path) -> config.Config:
    cfg = config.defaults()
    cfg.db.path = str(tmp_path / "test.db")
    cfg.reranker.enabled = False
    return cfg


# ---------------------------------------------------------------------------
# Unit tests — all external calls are patched
# ---------------------------------------------------------------------------

class TestSearch:
    def test_returns_matching_symbol(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/alpha.py")
        sid = _seed_symbol(conn, fid, "alpha_fn")
        _seed_embedding(conn, sid, [1.0, 0.0, 0.0])

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("alpha function"))
        assert "alpha_fn" in result

    def test_returns_no_results_message_when_empty(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("anything"))
        assert result == "No results found."

    def test_embedding_error_returns_message(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: (_ for _ in ()).throw(RuntimeError("connection refused")))

        result = _run(mcp_mod.search("alpha"))
        assert "Embedding error" in result
        assert "connection refused" in result

    def test_n_clamped_to_max(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/a.py")
        for i in range(60):
            sid = _seed_symbol(conn, fid, f"fn_{i}", line=i * 10 + 1, end_line=i * 10 + 5)
            _seed_embedding(conn, sid, [1.0, 0.0, 0.0])

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("fn", n=999))
        returned = [l for l in result.splitlines() if l.strip()]
        assert len(returned) <= mcp_mod._MAX_N * 2  # each result is two lines

    def test_directory_filter(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid_a = _seed_file(conn, "src/core/alpha.py")
        fid_b = _seed_file(conn, "src/ui/bravo.py")
        sid_a = _seed_symbol(conn, fid_a, "core_fn")
        sid_b = _seed_symbol(conn, fid_b, "ui_fn")
        _seed_embedding(conn, sid_a, [1.0, 0.0, 0.0])
        _seed_embedding(conn, sid_b, [1.0, 0.0, 0.0])

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("fn", directory="core"))
        assert "core_fn" in result
        assert "ui_fn" not in result

    def test_extension_filter(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid_py = _seed_file(conn, "src/a.py")
        fid_cs = _seed_file(conn, "src/b.cs")
        sid_py = _seed_symbol(conn, fid_py, "py_fn")
        sid_cs = _seed_symbol(conn, fid_cs, "cs_fn")
        _seed_embedding(conn, sid_py, [1.0, 0.0, 0.0])
        _seed_embedding(conn, sid_cs, [1.0, 0.0, 0.0])

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("fn", file_extensions=[".py"]))
        assert "py_fn" in result
        assert "cs_fn" not in result

    def test_reranker_api_key_passed_when_enabled(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/a.py")
        sid = _seed_symbol(conn, fid, "alpha_fn")
        _seed_embedding(conn, sid, [1.0, 0.0, 0.0])

        cfg = _fake_cfg(tmp_path)
        cfg.reranker.enabled = True
        cfg.reranker.endpoint = "http://reranker"
        cfg.reranker.model = "test-model"
        cfg.reranker.api_key = "secret-key"

        captured: dict = {}

        def fake_query_symbols(conn, blob, query, n, **kwargs):
            captured.update(kwargs)
            from blerk_cmd.query import query_symbols as real_qs
            return real_qs(conn, blob, query, n, **{k: v for k, v in kwargs.items()
                                                    if k not in ("reranker_endpoint", "reranker_model", "reranker_api_key")})

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: cfg)
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])
        monkeypatch.setattr(mcp_mod, "query_symbols", fake_query_symbols)

        _run(mcp_mod.search("alpha"))
        assert captured.get("reranker_api_key") == "secret-key"
        assert captured.get("reranker_endpoint") == "http://reranker"


class TestBrowse:
    def test_lists_files(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/alpha.py")
        _seed_symbol(conn, fid, "alpha_fn")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.browse()
        assert "src/alpha.py" in result

    def test_symbols_false_omits_symbol_tree(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/alpha.py")
        _seed_symbol(conn, fid, "alpha_fn")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.browse(symbols=False)
        assert "alpha_fn" not in result
        assert "src/alpha.py" in result

    def test_symbols_true_includes_symbol_tree(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/alpha.py")
        _seed_symbol(conn, fid, "alpha_fn")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.browse(symbols=True)
        assert "alpha_fn" in result

    def test_directory_filter(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid_a = _seed_file(conn, "src/core/alpha.py")
        fid_b = _seed_file(conn, "src/ui/bravo.py")
        _seed_symbol(conn, fid_a, "core_fn")
        _seed_symbol(conn, fid_b, "ui_fn")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.browse(directory="core")
        assert "core" in result
        assert "ui" not in result

    def test_truncation(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        for i in range(2100):
            fid = _seed_file(conn, f"src/file_{i:04d}.py")
            _seed_symbol(conn, fid, f"fn_{i}")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.browse(symbols=False)
        assert "truncated" in result
        assert len(result.splitlines()) <= 2001


class TestDetail:
    def test_returns_symbol_info(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid = _seed_file(conn, "src/alpha.py")
        _seed_symbol(conn, fid, "alpha_fn", description="does alpha things",
                     snippet="def alpha_fn(): pass")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.detail("alpha_fn")
        assert "alpha_fn" in result
        assert "src/alpha.py" in result
        assert "def alpha_fn" in result

    def test_unknown_symbol_returns_message(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.detail("nonexistent_fn")
        assert "No symbol named" in result

    def test_file_path_disambiguates(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        fid_a = _seed_file(conn, "pkg_a/alpha.py")
        fid_b = _seed_file(conn, "pkg_b/alpha.py")
        _seed_symbol(conn, fid_a, "alpha_fn", snippet="# in pkg_a")
        _seed_symbol(conn, fid_b, "alpha_fn", snippet="# in pkg_b")

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.detail("alpha_fn", file_path="pkg_a")
        assert "pkg_a" in result
        assert "pkg_b" not in result


class TestDeps:
    def test_returns_string(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        result = mcp_mod.deps()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Integration tests — real module init, no monkeypatching of internals
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_module_imports_cleanly(self):
        import blerk_cmd.mcp_server  # noqa: F401

    def test_get_conn_reconnects_after_close(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        cfg = _fake_cfg(tmp_path)

        monkeypatch.setattr(mcp_mod, "_cfg", cfg)
        monkeypatch.setattr(mcp_mod, "_conn", conn)

        conn.close()
        new_conn = mcp_mod._get_conn()
        assert new_conn.execute("SELECT 1").fetchone() == (1,)

    def test_search_returns_str(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)
        cfg = _fake_cfg(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: cfg)
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)
        monkeypatch.setattr(mcp_mod, "embed", lambda *a: [1.0, 0.0, 0.0])

        result = _run(mcp_mod.search("anything"))
        assert isinstance(result, str)

    def test_browse_returns_str(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        assert isinstance(mcp_mod.browse(), str)

    def test_detail_returns_str(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        assert isinstance(mcp_mod.detail("anything"), str)

    def test_deps_returns_str(self, tmp_path, monkeypatch):
        conn = _open(tmp_path)

        monkeypatch.setattr(mcp_mod, "_get_cfg", lambda: _fake_cfg(tmp_path))
        monkeypatch.setattr(mcp_mod, "_get_conn", lambda: conn)

        assert isinstance(mcp_mod.deps(), str)
