from __future__ import annotations

import pytest

from blerk import config, db
from blerk_cmd import antislop
from blerk_cmd.antislop import Scope, _fetch_symbols, _parse_response, reset_findings, sweep

_RULE_DESC = "The function looks confusing or pointless without additional context."


def _make_cfg() -> config.Config:
    cfg = config.defaults()
    cfg.antislop.endpoint = "http://localhost:11434"
    cfg.antislop.model = "llama3.2"
    return cfg


def _insert_symbol(
    conn,
    tmp_path,
    name: str,
    kind: str = "function",
    snippet: str = "def foo():\n    pass\n    pass\n    pass\n",
    params: str = "",
    end_line: int = 10,
) -> int:
    p = str(tmp_path / f"{name}.py")
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (p, 0, f"h_{name}"),
    )
    fid = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet, params)"
        " VALUES(?,?,?,?,?,?,?)",
        (fid, name, kind, 1, end_line, snippet, params),
    )
    return int(cur.lastrowid)


def _insert_ref(conn, caller_id: int, callee_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
        (caller_id, callee_id),
    )


def _rule_id(conn) -> int:
    return db.get_or_create_rule(conn, "antislop", "confusing", "warning", _RULE_DESC)


def _set_finding(conn, sid: int, rid: int, message: str, confidence: float) -> None:
    with db._write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO findings(symbol_id, rule_id, message, confidence)"
            " VALUES(?,?,?,?)",
            (sid, rid, message, confidence),
        )


def _get_finding(conn, sid: int, rid: int) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT message, confidence FROM findings WHERE symbol_id=? AND rule_id=?",
        (sid, rid),
    ).fetchone()
    return (row[0], row[1]) if row else None


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

def test_fetch_symbols_ordered_by_callers_then_size(conn, tmp_path):
    rid = _rule_id(conn)
    popular = _insert_symbol(conn, tmp_path, "popular", snippet="def popular():\n    pass\n")
    obscure = _insert_symbol(conn, tmp_path, "obscure",
                             snippet="def obscure():\n" + "    x = 1\n" * 20)
    mid = _insert_symbol(conn, tmp_path, "mid", snippet="def mid():\n    pass\n")

    for i in range(3):
        caller = _insert_symbol(conn, tmp_path, f"caller_{i}")
        _insert_ref(conn, caller, popular)
    caller_mid = _insert_symbol(conn, tmp_path, "caller_mid")
    _insert_ref(conn, caller_mid, mid)

    rows = _fetch_symbols(conn, 10, rid, Scope())
    names = [r[1] for r in rows if r[1] in ("popular", "mid", "obscure")]
    assert names.index("popular") < names.index("mid")
    assert names.index("mid") < names.index("obscure")


def test_fetch_symbols_size_breaks_caller_tie(conn, tmp_path):
    rid = _rule_id(conn)
    _insert_symbol(conn, tmp_path, "big", end_line=50)
    _insert_symbol(conn, tmp_path, "small", end_line=5)

    rows = _fetch_symbols(conn, 10, rid, Scope())
    names = [r[1] for r in rows if r[1] in ("big", "small")]
    assert names.index("big") < names.index("small")


# ---------------------------------------------------------------------------
# sweep behaviour tests
# ---------------------------------------------------------------------------

def test_clear_response_stores_finding(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "foo")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    finding = _get_finding(conn, sid, rid)
    assert finding is not None
    assert finding[0] == ""
    assert finding[1] == 0.0


def test_confusing_response_stores_finding_with_reason(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "bar")

    monkeypatch.setattr(
        antislop, "describe",
        lambda *a, **kw: "CONFUSING: This looks pointless.",
    )
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    finding = _get_finding(conn, sid, rid)
    assert finding is not None
    assert finding[0] == "This looks pointless."
    assert finding[1] == 1.0


def test_already_assessed_symbols_are_skipped(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "already")
    _set_finding(conn, sid, rid, "", 0.0)

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    assert len(calls) == 0


def test_symbols_without_snippet_are_skipped(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    p = str(tmp_path / "empty.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p, 0, "h"))
    fid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet)"
        " VALUES(?,?,?,?,?,?)",
        (fid, "nosnipper", "function", 1, 5, ""),
    )

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    assert len(calls) == 0


def test_n_limit_is_respected(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    for i in range(10):
        _insert_symbol(conn, tmp_path, f"sym{i}")

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")
    sweep(conn, cfg, n=3, scope=Scope(), rule_id=rid)

    assert len(calls) == 3


def test_dir_filter_works(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)

    sub = tmp_path / "subdir"
    sub.mkdir()

    p_in = str(sub / "inside.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p_in, 0, "h1"))
    fid_in = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet)"
        " VALUES(?,?,?,?,?,?)",
        (fid_in, "inside_fn", "function", 1, 10, "def inside_fn():\n    x=1\n    y=2\n    z=3\n"),
    )

    p_out = str(tmp_path / "outside.py")
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,?,?)", (p_out, 0, "h2"))
    fid_out = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet)"
        " VALUES(?,?,?,?,?,?)",
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
    sweep(conn, cfg, n=10, scope=Scope(directory=str(sub)), rule_id=rid)

    assert "inside_fn" in seen_names
    assert "outside_fn" not in seen_names


def test_malformed_response_does_not_store_finding(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "mystery")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "DUNNO")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    assert _get_finding(conn, sid, rid) is None


def test_output_contains_name_and_reason(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    _insert_symbol(conn, tmp_path, "weirdFunc")

    monkeypatch.setattr(
        antislop, "describe",
        lambda *a, **kw: "CONFUSING: This writes to a field that is never read.",
    )
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    captured = capsys.readouterr().out
    assert "weirdFunc" in captured
    assert "This writes to a field that is never read." in captured


def test_output_reports_assessed_and_skipped(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()
    rid = _rule_id(conn)

    sid_done = _insert_symbol(conn, tmp_path, "already_done")
    _set_finding(conn, sid_done, rid, "old reason", 1.0)

    _insert_symbol(conn, tmp_path, "new_sym")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    out = capsys.readouterr().out
    assert "1 already assessed" in out
    assert "Assessed 1" in out


def test_no_confusing_message_when_all_clear(conn, tmp_path, monkeypatch, capsys):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    _insert_symbol(conn, tmp_path, "cleanFunc")

    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    out = capsys.readouterr().out
    assert "No confusing fragments found." in out


def test_reset_clears_findings(conn, tmp_path):
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "old_sym")
    _set_finding(conn, sid, rid, "old reason", 1.0)

    reset_findings(conn, rid, Scope())

    assert _get_finding(conn, sid, rid) is None


def test_reset_does_not_affect_other_rules(conn, tmp_path):
    rid1 = _rule_id(conn)
    rid2 = db.get_or_create_rule(conn, "other", "other_rule", "warning", "Another rule.")
    sid = _insert_symbol(conn, tmp_path, "sym")
    _set_finding(conn, sid, rid1, "antislop finding", 1.0)
    _set_finding(conn, sid, rid2, "other finding", 0.9)

    reset_findings(conn, rid1, Scope())

    assert _get_finding(conn, sid, rid1) is None
    assert _get_finding(conn, sid, rid2) is not None


def test_already_assessed_skips_on_next_sweep(conn, tmp_path, monkeypatch):
    cfg = _make_cfg()
    rid = _rule_id(conn)
    sid = _insert_symbol(conn, tmp_path, "old_sym")
    _set_finding(conn, sid, rid, "old reason", 1.0)

    calls = []
    monkeypatch.setattr(antislop, "describe", lambda *a, **kw: calls.append(1) or "CLEAR")
    sweep(conn, cfg, n=10, scope=Scope(), rule_id=rid)

    assert len(calls) == 0
    assert _get_finding(conn, sid, rid)[0] == "old reason"
