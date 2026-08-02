from __future__ import annotations

import io
import sys

import pytest

from blerk import config
from blerk_cmd import antislop
from blerk_cmd.antislop import Scope, _fetch_symbols, _parse_response, reset_tags, sweep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(min_lines: int = 3) -> config.Config:
    cfg = config.defaults()
    cfg.symbolizer.min_describe_lines = min_lines
    cfg.antislop.endpoint = "http://localhost:11434"
    cfg.antislop.model = "llama3.2"
    return cfg


def _insert_symbol(conn, tmp_path, name: str, kind: str = "function",
                   snippet: str = "def foo():\n    pass\n    pass\n    pass\n",
                   params: str = "", end_line: int = 10) -> int:
    p = str(tmp_path / f"{name}.py")
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (p, 0, f"h_{name}"),
    )
    fid = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet, params) VALUES(?,?,?,?,?,?,?)",
        (fid, name, kind, 1, end_line, snippet, params),
    )
    return int(cur.lastrowid)


def _tag(conn, sid: int, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO symbol_tags(symbol_id, key, value) VALUES(?,?,?)",
        (sid, key, value),
    )


def _get_tags(conn, sid: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM symbol_tags WHERE symbol_id=?", (sid,)
    ).fetchall()
    return {k: v for k, v in rows}


# ---------------------------------------------------------------------------
# _parse_response tests
# ---------------------------------------------------------------------------

def test_parse_clear():
    is_c, reason = _parse_response("CLEAR")
    assert is_c is False
    assert reason == ""


def test_parse_clear_with_trailing():
    is_c, reason = _parse_response("CLEAR\nsome extra text")
    assert is_c is False


def test_parse_confusing():
    is_c, reason = _parse_response("CONFUSING: This function does nothing useful.")
    assert is_c is True
    assert reason == "This function does nothing useful."


def test_parse_malformed():
    is_c, reason = _parse_response("MAYBE?")
    assert is_c is None
    assert reason == ""


# ---------------------------------------------------------------------------
# _fetch_symbols ordering tests
# ---------------------------------------------------------------------------

def _insert_ref(conn, caller_id: int, callee_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
        (caller_id, callee_id),
    )


def test_fetch_symbols_ordered_by_callers_then_size(conn, tmp_path):
    # popular: 3 callers, short body
    popular = _insert_symbol(conn, tmp_path, "popular", snippet="def popular():\n    pass\n")
    # obscure: 0 callers, long body
    obscure = _insert_symbol(conn, tmp_path, "obscure",
                             snippet="def obscure():\n" + "    x = 1\n" * 20)
    # mid: 1 caller, short body
    mid = _insert_symbol(conn, tmp_path, "mid", snippet="def mid():\n    pass\n")

    # Add callers for popular (3 distinct callers) and mid (1 caller).
    for i in range(3):
        caller = _insert_symbol(conn, tmp_path, f"caller_{i}")
        _insert_ref(conn, caller, popular)
    caller_mid = _insert_symbol(conn, tmp_path, "caller_mid")
    _insert_ref(conn, caller_mid, mid)

    rows = _fetch_symbols(conn, 10, Scope())
    names = [r[1] for r in rows if r[1] in ("popular", "mid", "obscure")]
    assert names.index("popular") < names.index("mid")
    assert names.index("mid") < names.index("obscure")


def test_fetch_symbols_size_breaks_caller_tie(conn, tmp_path):
    # Both have 0 callers; big should come before small due to size (end_line - line).
    big = _insert_symbol(conn, tmp_path, "big", end_line=50)
    small = _insert_symbol(conn, tmp_path, "small", end_line=5)

    rows = _fetch_symbols(conn, 10, Scope())
    names = [r[1] for r in rows if r[1] in ("big", "small")]
    assert names.index("big") < names.index("small")


# ---------------------------------------------------------------------------
# sweep behaviour tests
# ---------------------------------------------------------------------------

def test_clear_response_stores_false_no_reason(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    sid = _insert_symbol(conn, tmp_path, "foo")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    tags = _get_tags(conn, sid)
    assert tags.get("confusing") == "false"
    assert "confusing_reason" not in tags


def test_confusing_response_stores_true_and_reason(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    sid = _insert_symbol(conn, tmp_path, "bar")

    monkeypatch.setattr(
        antislop, "describe",
        lambda *a, **kw: "CONFUSING: This looks pointless.",
    )

    sweep(conn, cfg, n=10, scope=Scope())

    tags = _get_tags(conn, sid)
    assert tags.get("confusing") == "true"
    assert tags.get("confusing_reason") == "This looks pointless."


def test_already_tagged_symbols_are_skipped(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    sid = _insert_symbol(conn, tmp_path, "already")
    _tag(conn, sid, "confusing", "false")

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    assert len(calls) == 0


def test_symbols_without_snippet_are_skipped(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    p = str(tmp_path / "empty.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p, 0, "h"))
    fid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid, "nosnipper", "function", 1, 5, ""),
    )

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    assert len(calls) == 0


def test_n_limit_is_respected(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    for i in range(10):
        _insert_symbol(conn, tmp_path, f"sym{i}")

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")

    sweep(conn, cfg, n=3, scope=Scope())

    assert len(calls) == 3


def test_dir_filter_works(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()

    sub = tmp_path / "subdir"
    sub.mkdir()

    p_in = str(sub / "inside.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p_in, 0, "h1"))
    fid_in = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid_in, "inside_fn", "function", 1, 10, "def inside_fn():\n    x=1\n    y=2\n    z=3\n"),
    )

    p_out = str(tmp_path / "outside.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p_out, 0, "h2"))
    fid_out = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid_out, "outside_fn", "function", 1, 10, "def outside_fn():\n    x=1\n    y=2\n    z=3\n"),
    )

    seen_names: list[str] = []

    def fake_describe(endpoint, model, api_key, prompt):
        if "inside_fn" in prompt:
            seen_names.append("inside_fn")
        elif "outside_fn" in prompt:
            seen_names.append("outside_fn")
        return "CLEAR"

    monkeypatch.setattr(antislop, "describe", fake_describe)

    sweep(conn, cfg, n=10, scope=Scope(directory=str(sub)))

    assert "inside_fn" in seen_names
    assert "outside_fn" not in seen_names


def test_malformed_response_does_not_store_tag(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    sid = _insert_symbol(conn, tmp_path, "mystery")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "DUNNO")

    sweep(conn, cfg, n=10, scope=Scope())

    tags = _get_tags(conn, sid)
    assert "confusing" not in tags


def test_output_contains_name_and_reason(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()
    _insert_symbol(conn, tmp_path, "weirdFunc")

    monkeypatch.setattr(
        antislop, "describe",
        lambda *a, **kw: "CONFUSING: This writes to a field that is never read.",
    )

    sweep(conn, cfg, n=10, scope=Scope())

    captured = capsys.readouterr().out
    assert "weirdFunc" in captured
    assert "This writes to a field that is never read." in captured


def test_output_reports_assessed_and_skipped(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()

    sid_tagged = _insert_symbol(conn, tmp_path, "already_done")
    _tag(conn, sid_tagged, "confusing", "true")
    _tag(conn, sid_tagged, "confusing_reason", "old reason")

    _insert_symbol(conn, tmp_path, "new_sym")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    out = capsys.readouterr().out
    assert "1 already tagged" in out
    assert "Assessed 1" in out


def test_no_confusing_message_when_all_clear(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()
    _insert_symbol(conn, tmp_path, "cleanFunc")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    out = capsys.readouterr().out
    assert "No confusing fragments found." in out


def test_reset_clears_existing_tags(conn, tmp_path):
    sid = _insert_symbol(conn, tmp_path, "old_sym")
    _tag(conn, sid, "confusing", "true")
    _tag(conn, sid, "confusing_reason", "old reason")

    reset_tags(conn, Scope())

    assert not _get_tags(conn, sid)


def test_reset_false_skips_already_tagged(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    sid = _insert_symbol(conn, tmp_path, "old_sym")
    _tag(conn, sid, "confusing", "true")
    _tag(conn, sid, "confusing_reason", "old reason")

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    assert len(calls) == 0
    assert _get_tags(conn, sid).get("confusing_reason") == "old reason"


def test_short_snippet_is_assessed(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    _insert_symbol(conn, tmp_path, "tiny", snippet="def tiny():\n    pass\n")

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")

    sweep(conn, cfg, n=10, scope=Scope())

    assert len(calls) == 1
