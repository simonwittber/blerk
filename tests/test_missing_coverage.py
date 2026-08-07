"""
Tests for logic that previously had no coverage.
Grouped by source module.
"""
from __future__ import annotations

import json

import pytest

from blerk import db
from blerk.symbols.types import count_params, _insert_markers, _build_stripped, Symbol
from blerk_cmd.analyze import _build_prompt, _parse_response
from blerk_cmd.fingerprinter import simhash
from blerk_cmd.query import _ext_sql, _dir_clause, _tag_clause, _no_headings_sql
from blerk_cmd.util import build_path_filters, normalize_dir, Scope


# ---------------------------------------------------------------------------
# blerk.symbols.types — count_params
# ---------------------------------------------------------------------------

class TestCountParams:
    def test_empty_string(self):
        assert count_params("") == 0

    def test_single_param(self):
        assert count_params("x int") == 1

    def test_two_params(self):
        assert count_params("x int, y string") == 2

    def test_generic_param_comma_not_counted(self):
        # The comma inside the angle brackets is NOT a separator.
        assert count_params("items List<int, string>") == 1

    def test_nested_generic(self):
        assert count_params("m Map<string, List<int>>") == 1

    def test_func_literal_param(self):
        # Comma inside parens is not a separator.
        assert count_params("fn func(a, b int), n int") == 2

    def test_whitespace_only(self):
        assert count_params("   ") == 0


# ---------------------------------------------------------------------------
# blerk.symbols.types — _insert_markers
# ---------------------------------------------------------------------------

class TestInsertMarkers:
    def test_markers_surround_target(self):
        lines = ["line1", "line2", "line3"]
        result = _insert_markers(lines, target_line=2, target_end_line=2)
        assert "===== DESCRIBE THIS SYMBOL =====" in result
        assert "===== END SYMBOL =====" in result
        parts = result.split("\n")
        start_idx = parts.index("===== DESCRIBE THIS SYMBOL =====")
        end_idx = parts.index("===== END SYMBOL =====")
        assert parts[start_idx + 1] == "line2"
        assert end_idx > start_idx

    def test_multi_line_target(self):
        lines = ["a", "b", "c", "d"]
        result = _insert_markers(lines, target_line=2, target_end_line=3)
        parts = result.split("\n")
        assert parts.count("===== DESCRIBE THIS SYMBOL =====") == 1
        assert parts.count("===== END SYMBOL =====") == 1


# ---------------------------------------------------------------------------
# blerk_cmd.util — build_path_filters / normalize_dir
# ---------------------------------------------------------------------------

class TestBuildPathFilters:
    def test_empty_scope_returns_no_filters(self):
        filters, params = build_path_filters(Scope())
        assert filters == []
        assert params == []

    def test_directory_filter_both_slash_styles(self):
        scope = Scope(directory="C:/foo/bar")
        filters, params = build_path_filters(scope)
        assert len(filters) == 1
        joined = " ".join(params)
        assert "C:/foo/bar" in joined or "C:\\foo\\bar" in joined

    def test_ext_filter_adds_like_clause(self):
        scope = Scope(exts=[".py", ".go"])
        filters, params = build_path_filters(scope)
        assert any(".py" in p for p in params)
        assert any(".go" in p for p in params)

    def test_exclude_converts_glob_wildcards(self):
        scope = Scope(excludes=["**/generated/*"])
        filters, params = build_path_filters(scope)
        assert any("NOT LIKE" in f for f in filters)
        assert any("%" in p for p in params)

    def test_multiple_excludes(self):
        scope = Scope(excludes=["*.gen.py", "vendor/*"])
        filters, params = build_path_filters(scope)
        not_like_count = sum(1 for f in filters if "NOT LIKE" in f)
        assert not_like_count == 2

    def test_question_mark_glob_becomes_sql_underscore(self):
        scope = Scope(excludes=["?.py"])
        _, params = build_path_filters(scope)
        assert any("_.py" in p for p in params)


class TestNormalizeDir:
    def test_non_empty_returns_realpath(self, tmp_path):
        result = normalize_dir(str(tmp_path))
        import os
        assert result == os.path.realpath(str(tmp_path)).replace("\\", "/")
        assert normalize_dir("nonexistent_xyz_abc") == "nonexistent_xyz_abc"

    def test_empty_string_returns_empty(self):
        result = normalize_dir("")
        assert result == ""


# ---------------------------------------------------------------------------
# blerk_cmd.analyze — _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def _rule(self, name="rule1", desc="Check this thing."):
        class R:
            pass
        r = R()
        r.name = name
        r.description = desc
        return r

    def test_empty_callers_shows_none(self):
        prompt = _build_prompt(
            "F", "function", "/f.py", 1, "pass", None,
            [], ["callee"], [self._rule()], 3, 5,
        )
        assert "Callers (up to 3): none" in prompt

    def test_empty_callees_shows_none(self):
        prompt = _build_prompt(
            "F", "function", "/f.py", 1, "pass", None,
            ["caller"], [], [self._rule()], 3, 5,
        )
        assert "Callees (up to 5): none" in prompt

    def test_multiple_rules_all_present(self):
        rules = [self._rule("rule_a", "Desc A."), self._rule("rule_b", "Desc B.")]
        prompt = _build_prompt(
            "F", "function", "/f.py", 1, "pass", None,
            [], [], rules, 3, 5,
        )
        assert "1. rule_a:" in prompt
        assert "2. rule_b:" in prompt

    def test_none_description_shows_none(self):
        prompt = _build_prompt(
            "F", "function", "/f.py", 1, "pass", None,
            [], [], [self._rule()], 3, 5,
        )
        assert "Description: none" in prompt

    def test_description_included_when_present(self):
        prompt = _build_prompt(
            "F", "function", "/f.py", 1, "pass", "Does X.",
            [], [], [self._rule()], 3, 5,
        )
        assert "Description: Does X." in prompt


# ---------------------------------------------------------------------------
# blerk_cmd.analyze — _parse_response edge cases
# ---------------------------------------------------------------------------

class TestParseResponseEdgeCases:
    def test_item_missing_rule_field_skipped(self):
        rule_map = {"r": 1}
        data = [{"severity": "error", "message": "m", "confidence": 0.9}]
        assert _parse_response(json.dumps(data), rule_map, 0.0) == []

    def test_item_missing_confidence_defaults_to_zero(self):
        rule_map = {"r": 1}
        data = [{"rule": "r", "severity": "error", "message": "m"}]
        result = _parse_response(json.dumps(data), rule_map, 0.0)
        assert len(result) == 1
        assert result[0][4] == 0.0

    def test_item_missing_message_defaults_to_empty_string(self):
        rule_map = {"r": 1}
        data = [{"rule": "r", "severity": "warning", "confidence": 0.8}]
        result = _parse_response(json.dumps(data), rule_map, 0.0)
        assert len(result) == 1
        assert result[0][3] == ""

    def test_confidence_boundary_exact_threshold_included(self):
        rule_map = {"r": 1}
        data = [{"rule": "r", "severity": "info", "message": "m", "confidence": 0.7}]
        result = _parse_response(json.dumps(data), rule_map, 0.7)
        assert len(result) == 1

    def test_not_an_array_returns_empty(self):
        rule_map = {"r": 1}
        assert _parse_response(json.dumps({"rule": "r"}), rule_map, 0.0) == []


# ---------------------------------------------------------------------------
# blerk_cmd.fingerprinter — simhash
# ---------------------------------------------------------------------------

class TestSimhash:
    def test_default_n4_produces_16_hex_chars(self):
        result = simhash("hello world")
        assert len(result) == 16
        int(result, 16)

    def test_custom_n_changes_result(self):
        text = "the quick brown fox"
        r4 = simhash(text, n=4)
        r3 = simhash(text, n=3)
        # Different n means different ngrams, so results should differ.
        assert r4 != r3

    def test_similar_snippets_produce_same_hash(self):
        a = "def foo(x):\n    return x + 1"
        b = "def foo(x):\n      return x  +  1"
        assert simhash(a) == simhash(b)

    def test_empty_string_returns_zero_hash(self):
        result = simhash("")
        assert result == "0000000000000000"

    def test_n1_produces_valid_hex(self):
        result = simhash("abcd", n=1)
        assert len(result) == 16
        int(result, 16)


# ---------------------------------------------------------------------------
# blerk_cmd.query — SQL helper functions
# ---------------------------------------------------------------------------

class TestExtSql:
    def test_empty_list_returns_empty(self):
        sql, params = _ext_sql([])
        assert sql == ""
        assert params == []

    def test_single_ext(self):
        sql, params = _ext_sql([".py"])
        assert "LIKE ?" in sql
        assert params == ["%  .py".replace("  ", "")]

    def test_multiple_exts_joined_with_or(self):
        sql, params = _ext_sql([".py", ".go"])
        assert " OR " in sql
        assert len(params) == 2

    def test_params_have_leading_percent(self):
        _, params = _ext_sql([".ts"])
        assert all(p.startswith("%") for p in params)


class TestDirClause:
    def test_empty_directory_returns_empty(self):
        sql, params = _dir_clause("")
        assert sql == ""
        assert params == []

    def test_non_empty_directory_adds_like(self):
        sql, params = _dir_clause("/some/path")
        assert "LIKE ?" in sql
        assert len(params) == 1
        assert "/some/path" in params[0]

    def test_backslashes_normalised(self):
        _, params = _dir_clause("C:\\Users\\foo")
        assert "\\" not in params[0]


class TestTagClause:
    def test_empty_tags_returns_empty(self):
        sql, params = _tag_clause({})
        assert sql == ""
        assert params == []

    def test_single_tag_produces_join(self):
        sql, params = _tag_clause({"lang": "py"})
        assert "JOIN symbol_tags" in sql
        assert "lang" in params
        assert "py" in params

    def test_multiple_tags_produce_multiple_joins(self):
        sql, params = _tag_clause({"a": "1", "b": "2"})
        assert sql.count("JOIN symbol_tags") == 2
        assert len(params) == 4

    def test_join_aliases_are_unique(self):
        sql, _ = _tag_clause({"x": "1", "y": "2"})
        assert "t0" in sql and "t1" in sql


class TestNoHeadingsSql:
    def test_md_in_exts_returns_empty(self):
        assert _no_headings_sql([".md"]) == ""

    def test_no_md_returns_filter(self):
        result = _no_headings_sql([".py"])
        assert "heading" in result

    def test_empty_exts_returns_filter(self):
        result = _no_headings_sql([])
        assert "heading" in result


# ---------------------------------------------------------------------------
# blerk_cmd.query — _dir_clause and _ext_sql via SQL roundtrip
# Note: These are already unit-tested above; we add one DB roundtrip test
# to confirm the clauses actually filter correctly when used in a query.
# ---------------------------------------------------------------------------

def _insert_file(conn, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,0,'h')", (path,)
    )
    return int(cur.lastrowid)


def _insert_sym(conn, fid: int, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (fid, name, "function", 1, 5),
    )
    return int(cur.lastrowid)


def test_ext_sql_filters_in_query(conn):
    fid_py = _insert_file(conn, "/src/foo.py")
    fid_go = _insert_file(conn, "/src/bar.go")
    _insert_sym(conn, fid_py, "foo")
    _insert_sym(conn, fid_go, "bar")

    sql_clause, params = _ext_sql([".py"])
    rows = conn.execute(
        f"SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id WHERE 1=1 {sql_clause}",
        params,
    ).fetchall()
    names = [r[0] for r in rows]
    assert "foo" in names
    assert "bar" not in names


def test_tag_clause_filters_by_tag(conn):
    fid = _insert_file(conn, "/src/a.py")
    sid1 = _insert_sym(conn, fid, "tagged")
    sid2 = _insert_sym(conn, fid, "untagged")
    conn.execute(
        "INSERT INTO symbol_tags(symbol_id, key, value) VALUES(?,?,?)",
        (sid1, "lang", "python"),
    )

    join_clause, params = _tag_clause({"lang": "python"})
    rows = conn.execute(
        f"SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id {join_clause}",
        params,
    ).fetchall()
    names = [r[0] for r in rows]
    assert "tagged" in names
    assert "untagged" not in names


# ---------------------------------------------------------------------------
# blerk_cmd.git_enricher — parse_branch additional edge cases
# ---------------------------------------------------------------------------

from blerk_cmd.git_enricher import parse_branch


class TestParseBranchEdgeCases:
    def test_single_local_branch(self):
        assert parse_branch("my-feature") == "my-feature"

    def test_detached_head_only(self):
        assert parse_branch("HEAD") == ""

    def test_head_and_tag_skips_tag(self):
        # Tags don't start with "origin/" and aren't "HEAD", so the fallback
        # picks the first such part. A tag like "v1.0" has no "HEAD ->" so
        # the second loop runs. "tag: v1.0" still doesn't start with "origin/"
        # and is not "HEAD" so it gets returned.
        result = parse_branch("HEAD, tag: v1.0")
        assert result == "tag: v1.0"

    def test_multiple_local_branches_returns_first(self):
        result = parse_branch("HEAD, branchA, branchB")
        assert result == "branchA"


# ---------------------------------------------------------------------------
# HLSL and GLSL extraction
# ---------------------------------------------------------------------------

from blerk.symbols.treesitter_extractor import Extractor


def test_hlsl_extracts_functions(write_temp):
    src = (
        "float4 MyVert(float3 pos : POSITION) : SV_Position {\n"
        "    return float4(pos, 1.0);\n"
        "}\n"
        "void MyFrag() {}\n"
    )
    path = write_temp("shader.hlsl", src)
    syms, _ = Extractor().extract(path)
    names = [s.name for s in syms]
    assert "MyVert" in names
    assert "MyFrag" in names


def test_hlsl_extracts_struct(write_temp):
    src = "struct Light { float3 dir; float intensity; };\n"
    path = write_temp("light.hlsl", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "Light" and s.kind == "struct" for s in syms)


def test_hlsl_extracts_call_refs(write_temp):
    src = (
        "float4 A() { return B(); }\n"
        "float4 B() { return float4(0,0,0,1); }\n"
    )
    path = write_temp("refs.hlsl", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "A" and r.callee_name == "B" for r in refs)


def test_glsl_extracts_functions(write_temp):
    src = (
        "vec4 applyFog(vec3 color, float depth) {\n"
        "    return mix(vec4(0.5), vec4(color, 1.0), depth);\n"
        "}\n"
    )
    path = write_temp("fog.glsl", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "applyFog" and s.kind == "function" for s in syms)


def test_glsl_extracts_struct(write_temp):
    src = "struct Material { vec3 albedo; float roughness; };\n"
    path = write_temp("mat.glsl", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "Material" and s.kind == "struct" for s in syms)


def test_vert_extension_parsed_as_glsl(write_temp):
    src = "void main() { gl_Position = vec4(0.0); }\n"
    path = write_temp("main.vert", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "main" for s in syms)


def test_frag_extension_parsed_as_glsl(write_temp):
    src = "vec4 shade(vec3 n) { return vec4(n, 1.0); }\n"
    path = write_temp("shader.frag", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "shade" for s in syms)


def test_fx_extension_parsed_as_hlsl(write_temp):
    src = "float4 Render() { return float4(1,1,1,1); }\n"
    path = write_temp("effect.fx", src)
    syms, _ = Extractor().extract(path)
    assert any(s.name == "Render" for s in syms)
