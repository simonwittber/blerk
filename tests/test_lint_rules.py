from __future__ import annotations

import pytest

from blerk_cmd.lint_rules import build_scope, fat_class, wide_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_file(conn, path: str) -> int:
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,0,'h')", (path,))
    return int(cur.lastrowid)


def _insert_symbol(conn, file_id: int, name: str, kind: str,
                   line: int, end_line: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (file_id, name, kind, line, end_line),
    )
    return int(cur.lastrowid)


def _insert_ref(conn, caller_id: int, callee_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO symbol_refs(caller_id, callee_id) VALUES(?,?)",
        (caller_id, callee_id),
    )


# ---------------------------------------------------------------------------
# fat_class tests
# ---------------------------------------------------------------------------

class TestFatClass:
    def test_class_over_threshold_flagged(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "BigClass", "class", 1, 120)
        for i in range(12):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert len(violations) == 1
        assert violations[0][2] == "fat_class"
        assert "BigClass" in violations[0][3]
        assert "12 methods" in violations[0][3]

    def test_class_at_threshold_not_flagged(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "OkClass", "class", 1, 110)
        for i in range(10):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert not violations

    def test_class_without_end_line_skipped(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "NoEnd", "class", 1, None)
        for i in range(15):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert not violations

    def test_methods_outside_line_range_not_counted(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "SmallClass", "class", 1, 20)
        for i in range(3):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 5, 5 + i * 5)
        # Methods outside the class range
        for i in range(15):
            _insert_symbol(conn, fid, f"other_{i}", "method", 100 + i * 5, 103 + i * 5)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert not violations

    def test_struct_flagged(self, conn):
        fid = _insert_file(conn, "src/a.go")
        _insert_symbol(conn, fid, "BigStruct", "struct", 1, 120)
        for i in range(12):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert any(v[2] == "fat_class" for v in violations)

    def test_interface_flagged(self, conn):
        fid = _insert_file(conn, "src/IFoo.cs")
        _insert_symbol(conn, fid, "IFoo", "interface", 1, 120)
        for i in range(12):
            _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert any(v[2] == "fat_class" for v in violations)

    def test_directory_filter(self, conn, tmp_path):
        fid_a = _insert_file(conn, str(tmp_path / "pkg_a" / "a.py"))
        fid_b = _insert_file(conn, str(tmp_path / "pkg_b" / "b.py"))
        for fid in (fid_a, fid_b):
            _insert_symbol(conn, fid, "BigClass", "class", 1, 120)
            for i in range(12):
                _insert_symbol(conn, fid, f"method_{i}", "method", 2 + i * 10, 8 + i * 10)

        build_scope(conn, str(tmp_path / "pkg_a"), [])
        violations = fat_class(conn, str(tmp_path / "pkg_a"), 10, [])
        paths = {v[0] for v in violations}
        assert all("pkg_a" in p for p in paths)
        assert not any("pkg_b" in p for p in paths)


# ---------------------------------------------------------------------------
# wide_module tests
# ---------------------------------------------------------------------------

class TestWideModule:
    def _setup_wide_file(self, conn, caller_path: str, n_callees: int):
        caller_fid = _insert_file(conn, caller_path)
        caller_sid = _insert_symbol(conn, caller_fid, "caller_fn", "function", 1)
        for i in range(n_callees):
            callee_fid = _insert_file(conn, f"src/dep_{i}.py")
            callee_sid = _insert_symbol(conn, callee_fid, f"dep_fn_{i}", "function", 1)
            _insert_ref(conn, caller_sid, callee_sid)
        return caller_fid

    def test_wide_file_flagged(self, conn):
        self._setup_wide_file(conn, "src/hub.py", 12)

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert len(violations) == 1
        assert violations[0][2] == "wide_module"
        assert "12 file dependencies" in violations[0][3]

    def test_narrow_file_not_flagged(self, conn):
        self._setup_wide_file(conn, "src/small.py", 5)

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert not violations

    def test_at_threshold_not_flagged(self, conn):
        self._setup_wide_file(conn, "src/mid.py", 10)

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert not violations

    def test_self_calls_not_counted(self, conn):
        fid = _insert_file(conn, "src/self.py")
        sids = [_insert_symbol(conn, fid, f"fn_{i}", "function", i + 1) for i in range(20)]
        for i in range(1, len(sids)):
            _insert_ref(conn, sids[0], sids[i])

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert not violations

    def test_multiple_callers_same_callee_counted_once(self, conn):
        caller_fid = _insert_file(conn, "src/a.py")
        callee_fid = _insert_file(conn, "src/b.py")
        for i in range(5):
            s = _insert_symbol(conn, caller_fid, f"fn_{i}", "function", i + 1)
            t = _insert_symbol(conn, callee_fid, f"dep_{i}", "function", i + 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert not violations

    def test_directory_filter(self, conn, tmp_path):
        pkg_a = str(tmp_path / "pkg_a")
        pkg_b = str(tmp_path / "pkg_b")

        caller_a_fid = _insert_file(conn, f"{pkg_a}/hub.py")
        caller_a_sid = _insert_symbol(conn, caller_a_fid, "fn", "function", 1)
        caller_b_fid = _insert_file(conn, f"{pkg_b}/hub.py")
        caller_b_sid = _insert_symbol(conn, caller_b_fid, "fn", "function", 1)

        for i in range(12):
            dep_fid = _insert_file(conn, f"shared/dep_{i}.py")
            dep_sid = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, caller_a_sid, dep_sid)
            _insert_ref(conn, caller_b_sid, dep_sid)

        build_scope(conn, pkg_a, [])
        violations = wide_module(conn, pkg_a, 10, [])
        paths = {v[0] for v in violations}
        assert all(pkg_a in p for p in paths)
        assert not any(pkg_b in p for p in paths)

    def test_no_refs_no_violations(self, conn):
        fid = _insert_file(conn, "src/isolated.py")
        _insert_symbol(conn, fid, "fn", "function", 1)

        build_scope(conn, "", [])
        violations = wide_module(conn, "", 10, [])
        assert not violations
