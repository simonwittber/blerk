from __future__ import annotations

import pytest

from blerk_cmd.lint_rules import (
    build_scope, fat_class, wide_module,
    wide_package, dep_spread, split_class, mixed_abstraction,
    duplicate_symbol, long_function,
)


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


# ---------------------------------------------------------------------------
# wide_package tests
# ---------------------------------------------------------------------------

class TestWidePackage:
    def test_fires_when_pkg_count_exceeds_threshold(self, conn):
        caller_fid = _insert_file(conn, "src/hub.py")
        s = _insert_symbol(conn, caller_fid, "hub_fn", "function", 1)
        for i in range(6):
            dep_fid = _insert_file(conn, f"pkg{i}/mod.py")
            t = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        violations = wide_package(conn, "", 5, [])
        assert len(violations) == 1
        assert violations[0][2] == "wide_package"
        assert "6 packages" in violations[0][3]

    def test_does_not_fire_at_threshold(self, conn):
        caller_fid = _insert_file(conn, "src/hub.py")
        s = _insert_symbol(conn, caller_fid, "hub_fn", "function", 1)
        for i in range(5):
            dep_fid = _insert_file(conn, f"pkg{i}/mod.py")
            t = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        assert not wide_package(conn, "", 5, [])

    def test_multiple_symbols_same_package_counted_once(self, conn):
        caller_fid = _insert_file(conn, "src/hub.py")
        s = _insert_symbol(conn, caller_fid, "hub_fn", "function", 1)
        for i in range(8):
            dep_fid = _insert_file(conn, f"shared/dep_{i}.py")
            t = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        assert not wide_package(conn, "", 5, [])

    def test_same_dir_calls_not_counted_as_separate_package(self, conn):
        fid = _insert_file(conn, "pkg/a.py")
        s = _insert_symbol(conn, fid, "fn_a", "function", 1)
        for i in range(6):
            dep_fid = _insert_file(conn, f"pkg/dep_{i}.py")
            t = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        assert not wide_package(conn, "", 5, [])


# ---------------------------------------------------------------------------
# dep_spread tests
# ---------------------------------------------------------------------------

class TestDepSpread:
    def test_fires_when_spread_high(self, conn):
        fid = _insert_file(conn, "src/thin.py")
        s = _insert_symbol(conn, fid, "fn", "function", 1)
        for i in range(6):
            dep_fid = _insert_file(conn, f"dep/d_{i}.py")
            t = _insert_symbol(conn, dep_fid, f"fn_{i}", "function", 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        violations = dep_spread(conn, "", 100, [])
        assert len(violations) == 1
        assert violations[0][2] == "dep_spread"

    def test_does_not_fire_when_spread_low(self, conn):
        fid = _insert_file(conn, "src/fat.py")
        dep_fid = _insert_file(conn, "dep/shared.py")
        for i in range(10):
            s = _insert_symbol(conn, fid, f"fn_{i}", "function", i + 1)
            t = _insert_symbol(conn, dep_fid, f"dep_{i}", "function", i + 1)
            _insert_ref(conn, s, t)

        build_scope(conn, "", [])
        assert not dep_spread(conn, "", 100, [])

    def test_zero_deps_no_violation(self, conn):
        fid = _insert_file(conn, "src/isolated.py")
        _insert_symbol(conn, fid, "fn", "function", 1)

        build_scope(conn, "", [])
        assert not dep_spread(conn, "", 10, [])


# ---------------------------------------------------------------------------
# split_class tests
# ---------------------------------------------------------------------------

class TestSplitClass:
    def test_disconnected_methods_flagged(self, conn):
        fid = _insert_file(conn, "src/mixed.py")
        _insert_symbol(conn, fid, "MyClass", "class", 1, 50)
        _insert_symbol(conn, fid, "group_a", "method", 2, 10)
        _insert_symbol(conn, fid, "group_b", "method", 12, 20)

        build_scope(conn, "", [])
        violations = split_class(conn, "", 2, [])
        assert len(violations) == 1
        assert violations[0][2] == "split_class"

    def test_connected_methods_not_flagged(self, conn):
        fid = _insert_file(conn, "src/cohesive.py")
        _insert_symbol(conn, fid, "MyClass", "class", 1, 50)
        m1 = _insert_symbol(conn, fid, "method_a", "method", 2, 10)
        m2 = _insert_symbol(conn, fid, "method_b", "method", 12, 20)
        _insert_ref(conn, m1, m2)

        build_scope(conn, "", [])
        assert not split_class(conn, "", 2, [])

    def test_single_method_class_not_flagged(self, conn):
        fid = _insert_file(conn, "src/tiny.py")
        _insert_symbol(conn, fid, "Tiny", "class", 1, 20)
        _insert_symbol(conn, fid, "only_method", "method", 2, 10)

        build_scope(conn, "", [])
        assert not split_class(conn, "", 2, [])

    def test_three_disconnected_groups_flagged(self, conn):
        fid = _insert_file(conn, "src/god.py")
        _insert_symbol(conn, fid, "GodClass", "class", 1, 100)
        _insert_symbol(conn, fid, "a1", "method", 2, 10)
        _insert_symbol(conn, fid, "b1", "method", 20, 30)
        _insert_symbol(conn, fid, "c1", "method", 40, 50)

        build_scope(conn, "", [])
        violations = split_class(conn, "", 2, [])
        assert len(violations) == 1
        assert "3" in violations[0][3]


# ---------------------------------------------------------------------------
# mixed_abstraction tests
# ---------------------------------------------------------------------------

class TestMixedAbstraction:
    def _make_high_inbound(self, conn, path: str, n_callers: int) -> int:
        dep_fid = _insert_file(conn, path)
        dep_sid = _insert_symbol(conn, dep_fid, "dep_fn", "function", 1)
        for i in range(n_callers):
            other_fid = _insert_file(conn, f"other_{i}_{path}")
            other_sid = _insert_symbol(conn, other_fid, f"other_fn_{i}", "function", 1)
            _insert_ref(conn, other_sid, dep_sid)
        return dep_sid

    def test_fires_when_mixing_high_and_low(self, conn):
        caller_fid = _insert_file(conn, "src/mixed.py")
        caller_sid = _insert_symbol(conn, caller_fid, "mixed_fn", "function", 1)
        for i in range(2):
            h_sid = self._make_high_inbound(conn, f"high_{i}.py", 5)
            _insert_ref(conn, caller_sid, h_sid)
        for i in range(2):
            low_fid = _insert_file(conn, f"low_{i}.py")
            low_sid = _insert_symbol(conn, low_fid, "low_fn", "function", 1)
            _insert_ref(conn, caller_sid, low_sid)

        build_scope(conn, "", [])
        violations = mixed_abstraction(conn, "", 2, [])
        assert len(violations) == 1
        assert violations[0][2] == "mixed_abstraction"
        assert "mixed_fn" in violations[0][3]
        assert violations[0][1] == 1

    def test_only_high_inbound_deps_no_violation(self, conn):
        caller_fid = _insert_file(conn, "src/pure.py")
        caller_sid = _insert_symbol(conn, caller_fid, "pure_fn", "function", 1)
        for i in range(3):
            h_sid = self._make_high_inbound(conn, f"util_{i}.py", 5)
            _insert_ref(conn, caller_sid, h_sid)

        build_scope(conn, "", [])
        assert not mixed_abstraction(conn, "", 2, [])

    def test_only_low_inbound_deps_no_violation(self, conn):
        caller_fid = _insert_file(conn, "src/leaf.py")
        caller_sid = _insert_symbol(conn, caller_fid, "leaf_fn", "function", 1)
        for i in range(3):
            low_fid = _insert_file(conn, f"detail_{i}.py")
            low_sid = _insert_symbol(conn, low_fid, "detail_fn", "function", 1)
            _insert_ref(conn, caller_sid, low_sid)

        build_scope(conn, "", [])
        assert not mixed_abstraction(conn, "", 2, [])


# ---------------------------------------------------------------------------
# duplicate_symbol tests (grouping and scores)
# ---------------------------------------------------------------------------

def _insert_fingerprint(conn, sid: int, kind: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fingerprints(symbol_id, kind, value) VALUES(?,?,?)",
        (sid, kind, value),
    )


class TestDuplicateSymbol:
    def test_exact_clone_three_files_gives_one_violation(self, conn):
        normhash = "aabbccdd" * 8  # 64 hex chars (32 bytes)
        for i in range(3):
            fid = _insert_file(conn, f"src/file_{i}.py")
            sid = _insert_symbol(conn, fid, "clone_fn", "function", 10)
            _insert_fingerprint(conn, sid, "normhash", normhash)

        build_scope(conn, "", [])
        violations = duplicate_symbol(conn, "", 3, [])
        exact = [v for v in violations if v[2] == "exact_clone"]
        assert len(exact) == 1
        assert "3 copies" in exact[0][3]
        assert exact[0][4] == 2.0

    def test_exact_clone_two_groups_two_violations(self, conn):
        for g, h in enumerate(["aa" * 32, "bb" * 32]):
            for i in range(2):
                fid = _insert_file(conn, f"src/g{g}_file_{i}.py")
                sid = _insert_symbol(conn, fid, f"fn_{g}", "function", 1)
                _insert_fingerprint(conn, sid, "normhash", h)

        build_scope(conn, "", [])
        violations = duplicate_symbol(conn, "", 3, [])
        exact = [v for v in violations if v[2] == "exact_clone"]
        assert len(exact) == 2

    def test_near_clone_three_symbols_gives_one_violation(self, conn):
        # Three simhashes differing by at most 1 bit from their neighbors.
        # A=1, B=3 (dist 1), C=7 (dist 1 from B, 2 from A); all within threshold=3.
        simhashes = [
            ("0000000000000001", "src/a.py"),
            ("0000000000000003", "src/b.py"),
            ("0000000000000007", "src/c.py"),
        ]
        for val, path in simhashes:
            fid = _insert_file(conn, path)
            sid = _insert_symbol(conn, fid, "near_fn", "function", 1)
            _insert_fingerprint(conn, sid, "simhash", val)

        build_scope(conn, "", [])
        violations = duplicate_symbol(conn, "", 3, [])
        near = [v for v in violations if v[2] == "near_clone"]
        assert len(near) == 1
        assert "3 symbols" in near[0][3]

    def test_near_clone_dist0_scores_2(self, conn):
        for val, path in [("0000000000000001", "src/p.py"),
                          ("0000000000000001", "src/q.py")]:
            fid = _insert_file(conn, path)
            sid = _insert_symbol(conn, fid, "fn", "function", 1)
            _insert_fingerprint(conn, sid, "simhash", val)

        build_scope(conn, "", [])
        near = [v for v in duplicate_symbol(conn, "", 3, []) if v[2] == "near_clone"]
        assert len(near) == 1
        assert near[0][4] == 2.0

    def test_no_clones_no_violations(self, conn):
        # These simhashes differ by 32 bits from each other, well above threshold=3.
        distinct_hashes = ["0000000000000000", "00000000ffffffff", "ffffffff00000000"]
        for i, simhash in enumerate(distinct_hashes):
            fid = _insert_file(conn, f"src/unique_{i}.py")
            sid = _insert_symbol(conn, fid, f"fn_{i}", "function", 1)
            _insert_fingerprint(conn, sid, "normhash", f"{'%02x' % (i + 1)}" * 32)
            _insert_fingerprint(conn, sid, "simhash", simhash)

        build_scope(conn, "", [])
        assert not duplicate_symbol(conn, "", 3, [])


# ---------------------------------------------------------------------------
# Score field tests
# ---------------------------------------------------------------------------

class TestViolationScores:
    def test_long_function_score_is_ratio(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "big_fn", "function", 1, 81)  # 80 lines

        build_scope(conn, "", [])
        violations = long_function(conn, "", 40, [])
        assert len(violations) == 1
        assert abs(violations[0][4] - 2.0) < 0.01  # 80 / 40 = 2.0

    def test_fat_class_score_is_ratio(self, conn):
        fid = _insert_file(conn, "src/a.py")
        _insert_symbol(conn, fid, "BigClass", "class", 1, 200)
        for i in range(20):
            _insert_symbol(conn, fid, f"m_{i}", "method", 2 + i * 9, 8 + i * 9)

        build_scope(conn, "", [])
        violations = fat_class(conn, "", 10, [])
        assert len(violations) == 1
        assert abs(violations[0][4] - 2.0) < 0.01  # 20 / 10 = 2.0
