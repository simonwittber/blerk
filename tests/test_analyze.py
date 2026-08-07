from __future__ import annotations

import json

import pytest

from blerk import config, db
from blerk_cmd import analyze
from blerk_cmd.analyze import (
    Finding,
    Scope,
    _build_prompt,
    _fetch_refs,
    _fetch_symbols,
    _parse_response,
    _reset_findings,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_file(conn, tmp_path, name: str) -> int:
    p = str(tmp_path / f"{name}.py")
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (p, 0, f"h_{name}"),
    )
    return int(cur.lastrowid)


def _insert_symbol(
    conn,
    tmp_path,
    name: str,
    kind: str = "function",
    snippet: str = "def foo():\n    x = 1\n    return x\n",
    end_line: int = 10,
    description: str | None = None,
) -> int:
    fid = _insert_file(conn, tmp_path, name)
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, description)"
        " VALUES(?,?,?,?,?,?)",
        (fid, name, kind, 1, end_line, description),
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, start_line, end_line)"
        " VALUES(?,?,?,?,?)",
        (sid, 0, snippet, 1, end_line),
    )
    return sid


def _insert_ref(conn, caller_id: int, callee_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
        (caller_id, callee_id),
    )


class _FakeRule:
    def __init__(self, name, severity="warning", description="A test rule."):
        self.name = name
        self.severity = severity
        self.description = description


class _FakeAnalyzer:
    def __init__(self, name="test", rules=None):
        self.name = name
        self.description = ""
        self.endpoint = "http://localhost:11434"
        self.model = "llama3.2"
        self.api_key = ""
        self.min_lines = 0
        self.kinds = ["function", "method"]
        self.extensions = []
        self.confidence = 0.7
        self.max_context_callers = 3
        self.max_context_callees = 5
        self.rules = rules or [_FakeRule("test_rule")]


def _rule_ids(conn, analyzer) -> dict[str, int]:
    result = db.ensure_analyzers(conn, [analyzer])
    return result[analyzer.name]


# ---------------------------------------------------------------------------
# _parse_response tests
# ---------------------------------------------------------------------------

def test_parse_valid_json():
    rule_map = {"my_rule": 1}
    response = json.dumps([{"rule": "my_rule", "severity": "error", "message": "bad", "confidence": 0.9}])
    results = _parse_response(response, rule_map, 0.7)
    assert len(results) == 1
    assert results[0] == (1, "my_rule", "error", "bad", 0.9)


def test_parse_strips_markdown_fences():
    rule_map = {"r": 1}
    raw = [{"rule": "r", "severity": "warning", "message": "msg", "confidence": 0.8}]
    response = f"```json\n{json.dumps(raw)}\n```"
    results = _parse_response(response, rule_map, 0.0)
    assert len(results) == 1


def test_parse_filters_low_confidence():
    rule_map = {"r": 1}
    response = json.dumps([{"rule": "r", "severity": "info", "message": "m", "confidence": 0.3}])
    assert _parse_response(response, rule_map, 0.7) == []


def test_parse_skips_unknown_rules():
    rule_map = {"known": 1}
    response = json.dumps([{"rule": "unknown", "severity": "error", "message": "m", "confidence": 0.9}])
    assert _parse_response(response, rule_map, 0.0) == []


def test_parse_invalid_json_returns_empty():
    assert _parse_response("not json at all", {"r": 1}, 0.0) == []


def test_parse_empty_array():
    assert _parse_response("[]", {"r": 1}, 0.0) == []


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

def test_build_prompt_contains_symbol_info():
    rules = [_FakeRule("my_rule", description="Check for bad things.")]
    prompt = _build_prompt(
        "MyFunc", "function", "/src/foo.py", 10,
        "def MyFunc():\n    pass",
        "Does nothing.",
        ["caller_a"], ["callee_b"],
        rules, 3, 5,
    )
    assert "MyFunc" in prompt
    assert "/src/foo.py" in prompt
    assert "line 10" in prompt
    assert "caller_a" in prompt
    assert "callee_b" in prompt
    assert "my_rule" in prompt
    assert "Check for bad things." in prompt
    assert "Does nothing." in prompt


def test_build_prompt_none_callers():
    rules = [_FakeRule("r")]
    prompt = _build_prompt("F", "function", "/f.py", 1, "pass", None, [], [], rules, 3, 5)
    assert "none" in prompt


# ---------------------------------------------------------------------------
# _fetch_refs tests
# ---------------------------------------------------------------------------

def test_fetch_refs_returns_callers_and_callees(conn, tmp_path):
    target = _insert_symbol(conn, tmp_path, "target")
    caller = _insert_symbol(conn, tmp_path, "caller")
    callee = _insert_symbol(conn, tmp_path, "callee")
    _insert_ref(conn, caller, target)
    _insert_ref(conn, target, callee)

    callers, callees = _fetch_refs(conn, target, 10, 10)
    assert "caller" in callers
    assert "callee" in callees


def test_fetch_refs_respects_limits(conn, tmp_path):
    target = _insert_symbol(conn, tmp_path, "target")
    for i in range(5):
        c = _insert_symbol(conn, tmp_path, f"caller_{i}")
        _insert_ref(conn, c, target)

    callers, _ = _fetch_refs(conn, target, 2, 10)
    assert len(callers) == 2


# ---------------------------------------------------------------------------
# _fetch_symbols tests
# ---------------------------------------------------------------------------

def test_fetch_symbols_excludes_already_analyzed(conn, tmp_path):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    rid = rid_map["r"]

    sid = _insert_symbol(conn, tmp_path, "sym")
    with db._write_lock:
        conn.execute(
            "INSERT INTO findings(symbol_id, rule_id, message, confidence) VALUES(?,?,?,?)",
            (sid, rid, "found", 0.9),
        )

    rows = _fetch_symbols(conn, [rid], Scope(), ["function"], 0, 0)
    assert all(r[0] != sid for r in rows)


def test_fetch_symbols_min_lines_filter(conn, tmp_path):
    short = _insert_symbol(conn, tmp_path, "short", end_line=3)
    long_ = _insert_symbol(conn, tmp_path, "long_", end_line=20)

    rows = _fetch_symbols(conn, [], Scope(), ["function"], 10, 0)
    ids = [r[0] for r in rows]
    assert long_ in ids
    assert short not in ids


# ---------------------------------------------------------------------------
# run tests
# ---------------------------------------------------------------------------

def _make_response(rule_name: str, message: str, confidence: float = 0.9) -> str:
    return json.dumps([{
        "rule": rule_name,
        "severity": "warning",
        "message": message,
        "confidence": confidence,
    }])


def test_run_stores_findings(conn, tmp_path, monkeypatch):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    sid = _insert_symbol(conn, tmp_path, "sym")

    monkeypatch.setattr(analyze, "describe", lambda *a, **kw: _make_response("r", "bad thing"))

    findings, checked = run(
        conn, analyzer, rid_map, Scope(), [], 0.7, 0, False,
        "http://localhost:11434", "llama3.2", "",
    )

    assert checked == 1
    assert len(findings) == 1
    assert findings[0].rule_name == "r"
    assert findings[0].message == "bad thing"

    row = conn.execute(
        "SELECT message FROM findings WHERE symbol_id=? AND rule_id=?",
        (sid, rid_map["r"]),
    ).fetchone()
    assert row is not None
    assert row[0] == "bad thing"


def test_run_no_save_skips_db(conn, tmp_path, monkeypatch):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    _insert_symbol(conn, tmp_path, "sym")

    monkeypatch.setattr(analyze, "describe", lambda *a, **kw: _make_response("r", "bad"))

    findings, _ = run(
        conn, analyzer, rid_map, Scope(), [], 0.7, 0, True,
        "http://localhost:11434", "llama3.2", "",
    )

    assert len(findings) == 1
    count = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert count == 0


def test_run_respects_rule_filter(conn, tmp_path, monkeypatch):
    rules = [_FakeRule("r1"), _FakeRule("r2")]
    analyzer = _FakeAnalyzer(rules=rules)
    rid_map = _rule_ids(conn, analyzer)
    _insert_symbol(conn, tmp_path, "sym")

    called_prompts: list[str] = []

    def fake_describe(endpoint, model, api_key, prompt):
        called_prompts.append(prompt)
        return _make_response("r1", "found r1")

    monkeypatch.setattr(analyze, "describe", fake_describe)

    run(conn, analyzer, rid_map, Scope(), ["r1"], 0.7, 0, True,
        "http://localhost:11434", "llama3.2", "")

    assert called_prompts
    assert "r1" in called_prompts[0]
    assert "r2" not in called_prompts[0]


def test_run_skips_already_analyzed(conn, tmp_path, monkeypatch):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    sid = _insert_symbol(conn, tmp_path, "sym")
    with db._write_lock:
        conn.execute(
            "INSERT INTO findings(symbol_id, rule_id, message, confidence) VALUES(?,?,?,?)",
            (sid, rid_map["r"], "existing", 0.9),
        )

    calls = []
    monkeypatch.setattr(analyze, "describe", lambda *a, **kw: calls.append(1) or "[]")

    run(conn, analyzer, rid_map, Scope(), [], 0.7, 0, False,
        "http://localhost:11434", "llama3.2", "")

    assert len(calls) == 0


# ---------------------------------------------------------------------------
# _reset_findings tests
# ---------------------------------------------------------------------------

def test_reset_findings_deletes_by_rule_ids(conn, tmp_path):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    sid = _insert_symbol(conn, tmp_path, "sym")
    with db._write_lock:
        conn.execute(
            "INSERT INTO findings(symbol_id, rule_id, message, confidence) VALUES(?,?,?,?)",
            (sid, rid_map["r"], "msg", 0.9),
        )

    _reset_findings(conn, list(rid_map.values()), Scope())

    count = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert count == 0


def test_reset_findings_with_directory_filter(conn, tmp_path):
    analyzer = _FakeAnalyzer(rules=[_FakeRule("r")])
    rid_map = _rule_ids(conn, analyzer)
    rid = rid_map["r"]

    sub = tmp_path / "sub"
    sub.mkdir()

    fid_in = int(conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (str(sub / "in.py"), 0, "h1"),
    ).lastrowid)
    sid_in = int(conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line)"
        " VALUES(?,?,?,?,?)",
        (fid_in, "sym_in", "function", 1, 5),
    ).lastrowid)
    conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, start_line, end_line)"
        " VALUES(?,?,?,?,?)",
        (sid_in, 0, "pass", 1, 5),
    )

    fid_out = int(conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (str(tmp_path / "out.py"), 0, "h2"),
    ).lastrowid)
    sid_out = int(conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line)"
        " VALUES(?,?,?,?,?)",
        (fid_out, "sym_out", "function", 1, 5),
    ).lastrowid)
    conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, start_line, end_line)"
        " VALUES(?,?,?,?,?)",
        (sid_out, 0, "pass", 1, 5),
    )

    with db._write_lock:
        conn.execute(
            "INSERT INTO findings(symbol_id, rule_id, message, confidence) VALUES(?,?,?,?)",
            (sid_in, rid, "in", 0.9),
        )
        conn.execute(
            "INSERT INTO findings(symbol_id, rule_id, message, confidence) VALUES(?,?,?,?)",
            (sid_out, rid, "out", 0.9),
        )

    _reset_findings(conn, [rid], Scope(directory=str(sub)))

    assert conn.execute(
        "SELECT COUNT(*) FROM findings WHERE symbol_id=?", (sid_in,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM findings WHERE symbol_id=?", (sid_out,)
    ).fetchone()[0] == 1
