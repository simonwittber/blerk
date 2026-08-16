from __future__ import annotations

import json
import sqlite3

import pytest

import blerk_cmd.mcp_server as mcp_mod
from blerk_cmd import extract_knowledge


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
        mcp_mod._seen_knowledge_ids.clear()
        original = mcp_mod._conn
        yield
        mcp_mod._seen_knowledge_ids.clear()
        mcp_mod._conn = original

    @pytest.fixture
    def db_conn(self, conn):
        mcp_mod._conn = conn
        return conn

    def _insert_knowledge(self, conn, concept, pattern, body):
        conn.execute(
            "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
            (concept, pattern, body, "explicit"),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_returns_empty_when_no_conn(self):
        mcp_mod._conn = None
        assert mcp_mod._hints_for_paths(["blerk/db.py"]) == ""

    def test_wide_pattern_fires_on_any_path(self, db_conn):
        self._insert_knowledge(db_conn, "tip", "**", "Wide hint body.")
        result = mcp_mod._hints_for_paths(["some/random/path.py"])
        assert "Wide hint body." in result

    def test_narrow_pattern_fires_on_matching_path(self, db_conn):
        self._insert_knowledge(db_conn, "narrow", "blerk/**", "Narrow hint body.")
        result = mcp_mod._hints_for_paths(["K:/blerk/blerk/db.py"])
        assert "Narrow hint body." in result

    def test_narrow_pattern_silent_on_non_matching_path(self, db_conn):
        self._insert_knowledge(db_conn, "narrow", "blerk/**", "Should not appear.")
        result = mcp_mod._hints_for_paths(["K:/other/project/foo.py"])
        assert result == ""

    def test_seen_hint_not_repeated(self, db_conn):
        self._insert_knowledge(db_conn, "once", "**", "Once body.")
        mcp_mod._hints_for_paths(["x.py"])
        result = mcp_mod._hints_for_paths(["x.py"])
        assert result == ""

    def test_session_reset_clears_seen(self, db_conn):
        self._insert_knowledge(db_conn, "reset", "**", "Resettable body.")
        mcp_mod._hints_for_paths(["x.py"])
        mcp_mod._seen_knowledge_ids.clear()
        result = mcp_mod._hints_for_paths(["x.py"])
        assert "Resettable body." in result

    def test_section_header_present(self, db_conn):
        self._insert_knowledge(db_conn, "hdr", "**", "Header test.")
        result = mcp_mod._hints_for_paths(["x.py"])
        assert result.startswith("\nHints:\n")

    def test_suppressed_hint_not_shown(self, db_conn):
        kid = self._insert_knowledge(db_conn, "gone", "**", "Should be hidden.")
        db_conn.execute("UPDATE knowledge SET suppressed_at=unixepoch() WHERE id=?", (kid,))
        db_conn.commit()
        result = mcp_mod._hints_for_paths(["x.py"])
        assert "Should be hidden." not in result

    def test_multiple_hints_all_returned(self, db_conn):
        self._insert_knowledge(db_conn, "a", "**", "Hint A.")
        self._insert_knowledge(db_conn, "b", "**", "Hint B.")
        result = mcp_mod._hints_for_paths(["x.py"])
        assert "Hint A." in result
        assert "Hint B." in result


# ---------------------------------------------------------------------------
# _call knowledge_store / knowledge_session_reset
# ---------------------------------------------------------------------------

class TestCallKnowledge:
    @pytest.fixture(autouse=True)
    def _setup(self, conn):
        mcp_mod._seen_knowledge_ids.clear()
        original = mcp_mod._conn
        mcp_mod._conn = conn
        yield
        mcp_mod._seen_knowledge_ids.clear()
        mcp_mod._conn = original

    def test_knowledge_store_inserts_row(self, conn):
        mcp_mod._call("knowledge_store", {"concept": "c", "pattern": "**", "body": "b"})
        row = conn.execute("SELECT concept, pattern, body FROM knowledge").fetchone()
        assert row == ("c", "**", "b")

    def test_knowledge_store_default_source_explicit(self, conn):
        mcp_mod._call("knowledge_store", {"concept": "c", "pattern": "**", "body": "b"})
        row = conn.execute("SELECT source FROM knowledge").fetchone()
        assert row[0] == "explicit"

    def test_knowledge_store_returns_confirmation(self):
        result = mcp_mod._call("knowledge_store", {"concept": "c", "pattern": "**", "body": "b"})
        assert "Knowledge stored" in result

    def test_knowledge_store_unavailable_without_conn(self):
        mcp_mod._conn = None
        result = mcp_mod._call("knowledge_store", {"concept": "c", "pattern": "**", "body": "b"})
        assert "unavailable" in result.lower()

    def test_knowledge_session_reset_clears_seen(self):
        mcp_mod._seen_knowledge_ids.add(99)
        mcp_mod._call("knowledge_session_reset", {})
        assert 99 not in mcp_mod._seen_knowledge_ids

    def test_knowledge_session_reset_returns_message(self):
        result = mcp_mod._call("knowledge_session_reset", {})
        assert "reset" in result.lower()


# ---------------------------------------------------------------------------
# search result includes knowledge section
# ---------------------------------------------------------------------------

class TestSearchWithKnowledge:
    @pytest.fixture(autouse=True)
    def _setup(self, conn, monkeypatch):
        mcp_mod._seen_knowledge_ids.clear()
        mcp_mod._conn = conn
        conn.execute(
            "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
            ("test-hint", "blerk/**", "Search hint body.", "explicit"),
        )
        conn.commit()
        fake_output = "  blerk/blerk_cmd/mcp_server.py:10-20\n"
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: fake_output)
        yield
        mcp_mod._seen_knowledge_ids.clear()
        mcp_mod._conn = None

    def test_hint_appended_to_search_output(self):
        result = mcp_mod._call("search", {"query": "x", "directory": "."})
        assert "Search hint body." in result

    def test_hint_not_repeated_on_second_search(self):
        mcp_mod._call("search", {"query": "x", "directory": "."})
        result = mcp_mod._call("search", {"query": "x", "directory": "."})
        assert "Search hint body." not in result


# ---------------------------------------------------------------------------
# extract_knowledge.filter_transcript
# ---------------------------------------------------------------------------

def _msg(role: str, content) -> dict:
    return {"type": role, "message": {"content": content}}


def _jsonl(*msgs) -> str:
    return "\n".join(json.dumps(m) for m in msgs) + "\n"


class TestParseTranscript:
    def test_reads_role_and_content(self):
        content = _jsonl(_msg("user", "hello"), _msg("assistant", "world"))
        result = extract_knowledge.filter_transcript(content, 9999)
        assert "user: hello" in result
        assert "assistant: world" in result

    def test_respects_max_chars(self):
        content = _jsonl(*[_msg("user", "x" * 100) for _ in range(20)])
        result = extract_knowledge.filter_transcript(content, 200)
        assert len(result) <= 300

    def test_handles_list_content(self):
        content = _jsonl(_msg("user", [{"type": "text", "text": "hi"}]))
        result = extract_knowledge.filter_transcript(content, 9999)
        assert "user: hi" in result

    def test_skips_non_user_assistant(self):
        content = _jsonl({"type": "permission-mode", "permissionMode": "default"})
        assert extract_knowledge.filter_transcript(content, 9999) == ""

    def test_empty_content_returns_empty(self):
        assert extract_knowledge.filter_transcript("", 9999) == ""

    def test_skips_invalid_json_lines(self):
        content = "not json\n" + json.dumps(_msg("user", "ok")) + "\n"
        result = extract_knowledge.filter_transcript(content, 9999)
        assert "user: ok" in result


# ---------------------------------------------------------------------------
# extract_knowledge.parse_knowledge
# ---------------------------------------------------------------------------

class TestParseKnowledge:
    def test_extracts_valid_items(self):
        text = json.dumps([
            {"concept": "c1", "pattern": "blerk/**", "body": "body one"},
            {"concept": "c2", "pattern": "**", "body": "body two"},
        ])
        items = extract_knowledge.parse_knowledge(text)
        assert len(items) == 2
        assert items[0]["concept"] == "c1"
        assert items[1]["body"] == "body two"

    def test_handles_markdown_fences(self):
        text = "Some prose.\n```json\n" + json.dumps([{"concept": "c", "body": "b"}]) + "\n```"
        items = extract_knowledge.parse_knowledge(text)
        assert len(items) == 1
        assert items[0]["concept"] == "c"

    def test_returns_empty_on_no_json_array(self):
        assert extract_knowledge.parse_knowledge("no array here") == []

    def test_returns_empty_on_bad_json(self):
        assert extract_knowledge.parse_knowledge("[bad json}") == []

    def test_skips_items_missing_concept_or_body(self):
        text = json.dumps([
            {"concept": "ok", "body": "present"},
            {"concept": "no-body"},
            {"body": "no-concept"},
        ])
        items = extract_knowledge.parse_knowledge(text)
        assert len(items) == 1
        assert items[0]["concept"] == "ok"

    def test_default_pattern_is_wide(self):
        text = json.dumps([{"concept": "c", "body": "b"}])
        items = extract_knowledge.parse_knowledge(text)
        assert items[0]["pattern"] == "**"


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

def test_knowledge_columns(conn):
    cols = [row[1] for row in conn.execute("PRAGMA table_info(knowledge)").fetchall()]
    for col in ("suppressed_at", "refined_at", "surfaced_count", "fact_checked_at"):
        assert col in cols, f"missing column: {col}"
