from __future__ import annotations

import json
import sqlite3

import pytest

import blerk_cmd.mcp_server as mcp_mod
from blerk_cmd import hint_extractor


# ---------------------------------------------------------------------------
# _pattern_matches
# ---------------------------------------------------------------------------

class TestPatternMatches:
    def test_relative_pattern_matches_absolute_path(self):
        assert mcp_mod._pattern_matches("K:/blerk/blerk_cmd/reindex.py", "blerk/**")

    def test_relative_pattern_matches_nested(self):
        assert mcp_mod._pattern_matches("K:/blerk/blerk/db.py", "blerk/blerk/**")

    def test_wide_pattern_matches_anything(self):
        assert mcp_mod._pattern_matches("/any/path/file.py", "**")

    def test_no_match_wrong_prefix(self):
        assert not mcp_mod._pattern_matches("K:/other/project/foo.py", "blerk/**")

    def test_single_file_pattern(self):
        assert mcp_mod._pattern_matches("K:/blerk/blerk_cmd/mcp_server.py", "blerk_cmd/mcp_server.py")

    def test_star_extension_match(self):
        assert mcp_mod._pattern_matches("K:/blerk/blerk/config.py", "blerk/*.py")


# ---------------------------------------------------------------------------
# _hints_for_paths
# ---------------------------------------------------------------------------

class TestHintsForPaths:
    @pytest.fixture(autouse=True)
    def _reset(self):
        mcp_mod._seen_hint_ids.clear()
        original = mcp_mod._conn
        yield
        mcp_mod._seen_hint_ids.clear()
        mcp_mod._conn = original

    @pytest.fixture
    def db_conn(self, conn):
        mcp_mod._conn = conn
        return conn

    def _insert_hint(self, conn, concept, pattern, body):
        conn.execute(
            "INSERT INTO hints(concept, pattern, body, source) VALUES (?,?,?,?)",
            (concept, pattern, body, "explicit"),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_returns_empty_when_no_conn(self):
        mcp_mod._conn = None
        assert mcp_mod._hints_for_paths(["blerk/db.py"]) == ""

    def test_wide_pattern_fires_on_any_path(self, db_conn):
        self._insert_hint(db_conn, "tip", "**", "Wide hint body.")
        result = mcp_mod._hints_for_paths(["some/random/path.py"])
        assert "Wide hint body." in result

    def test_narrow_pattern_fires_on_matching_path(self, db_conn):
        self._insert_hint(db_conn, "narrow", "blerk/**", "Narrow hint body.")
        result = mcp_mod._hints_for_paths(["K:/blerk/blerk/db.py"])
        assert "Narrow hint body." in result

    def test_narrow_pattern_silent_on_non_matching_path(self, db_conn):
        self._insert_hint(db_conn, "narrow", "blerk/**", "Should not appear.")
        result = mcp_mod._hints_for_paths(["K:/other/project/foo.py"])
        assert result == ""

    def test_seen_hint_not_repeated(self, db_conn):
        self._insert_hint(db_conn, "once", "**", "Once body.")
        mcp_mod._hints_for_paths(["x.py"])
        result = mcp_mod._hints_for_paths(["x.py"])
        assert result == ""

    def test_session_reset_clears_seen(self, db_conn):
        self._insert_hint(db_conn, "reset", "**", "Resettable body.")
        mcp_mod._hints_for_paths(["x.py"])
        mcp_mod._seen_hint_ids.clear()
        result = mcp_mod._hints_for_paths(["x.py"])
        assert "Resettable body." in result

    def test_section_header_present(self, db_conn):
        self._insert_hint(db_conn, "hdr", "**", "Header test.")
        result = mcp_mod._hints_for_paths(["x.py"])
        assert result.startswith("\nRelevant hints:\n")

    def test_multiple_hints_all_returned(self, db_conn):
        self._insert_hint(db_conn, "a", "**", "Hint A.")
        self._insert_hint(db_conn, "b", "**", "Hint B.")
        result = mcp_mod._hints_for_paths(["x.py"])
        assert "Hint A." in result
        assert "Hint B." in result


# ---------------------------------------------------------------------------
# _call hint_store / hint_session_reset
# ---------------------------------------------------------------------------

class TestCallHints:
    @pytest.fixture(autouse=True)
    def _setup(self, conn):
        mcp_mod._seen_hint_ids.clear()
        original = mcp_mod._conn
        mcp_mod._conn = conn
        yield
        mcp_mod._seen_hint_ids.clear()
        mcp_mod._conn = original

    def test_hint_store_inserts_row(self, conn):
        mcp_mod._call("hint_store", {"concept": "c", "pattern": "**", "body": "b"})
        row = conn.execute("SELECT concept, pattern, body FROM hints").fetchone()
        assert row == ("c", "**", "b")

    def test_hint_store_default_source_explicit(self, conn):
        mcp_mod._call("hint_store", {"concept": "c", "pattern": "**", "body": "b"})
        row = conn.execute("SELECT source FROM hints").fetchone()
        assert row[0] == "explicit"

    def test_hint_store_returns_confirmation(self):
        result = mcp_mod._call("hint_store", {"concept": "c", "pattern": "**", "body": "b"})
        assert "Hint stored" in result

    def test_hint_store_unavailable_without_conn(self):
        mcp_mod._conn = None
        result = mcp_mod._call("hint_store", {"concept": "c", "pattern": "**", "body": "b"})
        assert "unavailable" in result.lower()

    def test_hint_session_reset_clears_seen(self):
        mcp_mod._seen_hint_ids.add(99)
        mcp_mod._call("hint_session_reset", {})
        assert 99 not in mcp_mod._seen_hint_ids

    def test_hint_session_reset_returns_message(self):
        result = mcp_mod._call("hint_session_reset", {})
        assert "reset" in result.lower()


# ---------------------------------------------------------------------------
# search result includes hints section
# ---------------------------------------------------------------------------

class TestSearchWithHints:
    @pytest.fixture(autouse=True)
    def _setup(self, conn, monkeypatch):
        mcp_mod._seen_hint_ids.clear()
        mcp_mod._conn = conn
        conn.execute(
            "INSERT INTO hints(concept, pattern, body, source) VALUES (?,?,?,?)",
            ("test-hint", "blerk/**", "Search hint body.", "explicit"),
        )
        conn.commit()
        fake_output = "  blerk/blerk_cmd/mcp_server.py:10-20\n"
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: fake_output)
        yield
        mcp_mod._seen_hint_ids.clear()
        mcp_mod._conn = None

    def test_hint_appended_to_search_output(self):
        result = mcp_mod._call("search", {"query": "x", "directory": "."})
        assert "Search hint body." in result

    def test_hint_not_repeated_on_second_search(self):
        mcp_mod._call("search", {"query": "x", "directory": "."})
        result = mcp_mod._call("search", {"query": "x", "directory": "."})
        assert "Search hint body." not in result


# ---------------------------------------------------------------------------
# hint_extractor._read_transcript
# ---------------------------------------------------------------------------

class TestReadTranscript:
    def test_reads_role_and_content(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "assistant", "content": "world"}) + "\n",
            encoding="utf-8",
        )
        result = hint_extractor._read_transcript(str(f), 9999)
        assert "user: hello" in result
        assert "assistant: world" in result

    def test_respects_max_chars(self, tmp_path):
        f = tmp_path / "t.jsonl"
        lines = [json.dumps({"role": "user", "content": "x" * 100}) for _ in range(20)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = hint_extractor._read_transcript(str(f), 200)
        assert len(result) <= 300  # rough upper bound

    def test_skips_non_string_content(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n",
            encoding="utf-8",
        )
        result = hint_extractor._read_transcript(str(f), 9999)
        assert result == ""

    def test_missing_file_returns_empty(self):
        result = hint_extractor._read_transcript("/nonexistent/path.jsonl", 9999)
        assert result == ""

    def test_skips_invalid_json_lines(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n" + json.dumps({"role": "user", "content": "ok"}) + "\n", encoding="utf-8")
        result = hint_extractor._read_transcript(str(f), 9999)
        assert "user: ok" in result


# ---------------------------------------------------------------------------
# hint_extractor._parse_hints
# ---------------------------------------------------------------------------

class TestParseHints:
    def test_extracts_valid_hints(self):
        text = json.dumps([
            {"concept": "c1", "pattern": "blerk/**", "body": "body one"},
            {"concept": "c2", "pattern": "**", "body": "body two"},
        ])
        hints = hint_extractor._parse_hints(text)
        assert len(hints) == 2
        assert hints[0]["concept"] == "c1"
        assert hints[1]["body"] == "body two"

    def test_handles_markdown_fences(self):
        text = "Some prose.\n```json\n" + json.dumps([{"concept": "c", "body": "b"}]) + "\n```"
        hints = hint_extractor._parse_hints(text)
        assert len(hints) == 1
        assert hints[0]["concept"] == "c"

    def test_returns_empty_on_no_json_array(self):
        assert hint_extractor._parse_hints("no array here") == []

    def test_returns_empty_on_bad_json(self):
        assert hint_extractor._parse_hints("[bad json}") == []

    def test_skips_items_missing_concept_or_body(self):
        text = json.dumps([
            {"concept": "ok", "body": "present"},
            {"concept": "no-body"},
            {"body": "no-concept"},
        ])
        hints = hint_extractor._parse_hints(text)
        assert len(hints) == 1
        assert hints[0]["concept"] == "ok"

    def test_default_pattern_is_wide(self):
        text = json.dumps([{"concept": "c", "body": "b"}])
        hints = hint_extractor._parse_hints(text)
        assert hints[0]["pattern"] == "**"
