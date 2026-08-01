from __future__ import annotations

import pytest

from blerk import db
from blerk_cmd.fingerprinter import fingerprint, normhash, simhash
from blerk_cmd.lint_rules import duplicate_symbol


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    return db.open_db(str(tmp_path / "test.db"))


def _insert_symbol(conn, path: str, name: str, snippet: str, kind: str = "function") -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (path, 0, f"h_{name}"),
    )
    fid = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (fid, name, kind, 1, 10, snippet),
    )
    return int(cur.lastrowid)


def _fingerprint_symbol(conn, sid: int, snippet: str) -> None:
    fps = fingerprint(snippet)
    for kind, value in fps.items():
        conn.execute(
            "INSERT OR REPLACE INTO fingerprints(symbol_id, kind, value) VALUES(?,?,?)",
            (sid, kind, value),
        )


# ---------------------------------------------------------------------------
# normhash tests
# ---------------------------------------------------------------------------

def test_normhash_identical_snippets_match():
    s = "def foo(x):\n    return x + 1"
    assert normhash(s) == normhash(s)


def test_normhash_whitespace_normalised():
    a = "def foo(x):\n    return x + 1"
    b = "def foo(x):\n      return x  +  1"
    assert normhash(a) == normhash(b)


def test_normhash_case_normalised():
    a = "def Foo():\n    RETURN 1"
    b = "def foo():\n    return 1"
    assert normhash(a) == normhash(b)


def test_normhash_different_snippets_differ():
    a = "def foo(x):\n    return x + 1"
    b = "def bar(x):\n    return x * 2"
    assert normhash(a) != normhash(b)


def test_normhash_returns_hex_string():
    h = normhash("def foo(): pass")
    assert len(h) == 64
    int(h, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# simhash tests
# ---------------------------------------------------------------------------

def test_simhash_identical_snippets_distance_zero():
    s = "def foo(x):\n    return x + 1"
    h_a = int(simhash(s), 16)
    h_b = int(simhash(s), 16)
    assert bin(h_a ^ h_b).count("1") == 0


def test_simhash_similar_snippets_low_distance():
    a = "def foo(x):\n    return x + 1\n" * 10
    b = "def foo(x):\n    return x + 1\n" * 9 + "def foo(x):\n    return x + 2\n"
    h_a = int(simhash(a), 16)
    h_b = int(simhash(b), 16)
    assert bin(h_a ^ h_b).count("1") < 10


def test_simhash_very_different_snippets_high_distance():
    a = "def foo(x):\n    return x + 1"
    b = "class DatabaseConnectionPoolManager:\n    def execute_query(self, sql, params): pass"
    h_a = int(simhash(a), 16)
    h_b = int(simhash(b), 16)
    assert bin(h_a ^ h_b).count("1") > 5


def test_simhash_returns_16_char_hex():
    h = simhash("def foo(): pass")
    assert len(h) == 16
    int(h, 16)


def test_simhash_empty_snippet():
    h = simhash("")
    assert len(h) == 16


# ---------------------------------------------------------------------------
# fingerprint function tests
# ---------------------------------------------------------------------------

def test_fingerprint_returns_both_kinds():
    fps = fingerprint("def foo(): pass")
    assert "normhash" in fps
    assert "simhash" in fps


# ---------------------------------------------------------------------------
# duplicate_symbol lint rule tests
# ---------------------------------------------------------------------------

SNIPPET_A = "def foo(x):\n    return x + 1\n" * 5
SNIPPET_B = "def bar(y):\n    result = y * 2\n    return result\n" * 5


def test_exact_clone_detected(tmp_path):
    conn = _make_db(tmp_path)
    sid_a = _insert_symbol(conn, str(tmp_path / "a.py"), "foo", SNIPPET_A)
    sid_b = _insert_symbol(conn, str(tmp_path / "b.py"), "foo_copy", SNIPPET_A)
    _fingerprint_symbol(conn, sid_a, SNIPPET_A)
    _fingerprint_symbol(conn, sid_b, SNIPPET_A)

    violations = duplicate_symbol(conn, "", 3, [])
    rules = [v[2] for v in violations]
    assert "exact_clone" in rules


def test_no_clone_when_snippets_differ(tmp_path):
    conn = _make_db(tmp_path)
    sid_a = _insert_symbol(conn, str(tmp_path / "a.py"), "foo", SNIPPET_A)
    sid_b = _insert_symbol(conn, str(tmp_path / "b.py"), "bar", SNIPPET_B)
    _fingerprint_symbol(conn, sid_a, SNIPPET_A)
    _fingerprint_symbol(conn, sid_b, SNIPPET_B)

    violations = duplicate_symbol(conn, "", 3, [])
    assert not violations


def test_exact_clone_not_flagged_within_same_file(tmp_path):
    conn = _make_db(tmp_path)
    path = str(tmp_path / "a.py")
    sid_a = _insert_symbol(conn, path, "foo", SNIPPET_A)
    # Same file, same snippet - should not flag.
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, snippet) VALUES(?,?,?,?,?,?)",
        (conn.execute("SELECT file_id FROM symbols WHERE id=?", (sid_a,)).fetchone()[0],
         "foo2", "function", 20, 30, SNIPPET_A),
    )
    sid_b = int(cur.lastrowid)
    _fingerprint_symbol(conn, sid_a, SNIPPET_A)
    _fingerprint_symbol(conn, sid_b, SNIPPET_A)

    violations = duplicate_symbol(conn, "", 3, [])
    exact = [v for v in violations if v[2] == "exact_clone"]
    assert not exact


def test_near_clone_detected(tmp_path):
    conn = _make_db(tmp_path)
    # Build two long snippets that differ by one character.
    base = "def process_items(items):\n    result = []\n    for item in items:\n        result.append(item)\n    return result\n" * 6
    near = base[:-2] + "x\n"
    sid_a = _insert_symbol(conn, str(tmp_path / "a.py"), "process_items", base)
    sid_b = _insert_symbol(conn, str(tmp_path / "b.py"), "process_items_copy", near)
    _fingerprint_symbol(conn, sid_a, base)
    _fingerprint_symbol(conn, sid_b, near)

    violations = duplicate_symbol(conn, "", 3, [])
    rules = [v[2] for v in violations]
    assert "near_clone" in rules or "exact_clone" in rules


def test_near_clone_disabled_when_threshold_negative(tmp_path):
    conn = _make_db(tmp_path)
    base = "def foo(x):\n    return x\n" * 8
    near = base[:-1] + "y\n"
    sid_a = _insert_symbol(conn, str(tmp_path / "a.py"), "foo", base)
    sid_b = _insert_symbol(conn, str(tmp_path / "b.py"), "foo2", near)
    _fingerprint_symbol(conn, sid_a, base)
    _fingerprint_symbol(conn, sid_b, near)

    violations = duplicate_symbol(conn, "", -1, [])
    near_clones = [v for v in violations if v[2] == "near_clone"]
    assert not near_clones


def test_directory_filter_scopes_results(tmp_path):
    sub_a = tmp_path / "pkg_a"
    sub_b = tmp_path / "pkg_b"
    sub_a.mkdir()
    sub_b.mkdir()
    conn = _make_db(tmp_path)
    sid_a = _insert_symbol(conn, str(sub_a / "a.py"), "foo", SNIPPET_A)
    sid_b = _insert_symbol(conn, str(sub_b / "b.py"), "foo_copy", SNIPPET_A)
    _fingerprint_symbol(conn, sid_a, SNIPPET_A)
    _fingerprint_symbol(conn, sid_b, SNIPPET_A)

    violations = duplicate_symbol(conn, str(sub_a), 3, [])
    # Only a.py is in the scoped directory; no pair exists within it.
    assert not violations
