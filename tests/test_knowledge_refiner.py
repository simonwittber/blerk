from __future__ import annotations

import pytest

import blerk_cmd.extract_knowledge as ek
import blerk_cmd.knowledge_refiner as kr
import blerk_cmd.query as query_mod
from blerk.config import Config, Knowledge, LLM, KnowledgeRefiner


def _make_cfg() -> Config:
    cfg = Config()
    cfg.knowledge = Knowledge(
        llm=LLM(endpoint="http://test", model="test-model", api_key=""),
    )
    return cfg


def _insert_knowledge(conn, concept="tip", pattern="**", body="Some fact.") -> int:
    cur = conn.execute(
        "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
        (concept, pattern, body, "auto"),
    )
    kid = cur.lastrowid
    conn.commit()
    return kid


def _row(concept="tip", pattern="**", body="Some fact.", id_=1) -> dict:
    return {
        "id": id_, "concept": concept, "pattern": pattern, "body": body,
        "source": "auto", "created_at": 0, "surfaced_count": 0, "refined_at": None,
    }


# ---------------------------------------------------------------------------
# _resolve_cfg
# ---------------------------------------------------------------------------

class TestResolveCfg:
    def test_noop_when_prompt_set(self):
        r = KnowledgeRefiner(type="fact-check", prompt_template="custom prompt")
        assert kr._resolve_cfg(r).prompt_template == "custom prompt"

    def test_uses_registered_default_when_empty(self):
        r = KnowledgeRefiner(type="fact-check", prompt_template="")
        resolved = kr._resolve_cfg(r)
        assert "{body}" in resolved.prompt_template
        assert "{snippets}" in resolved.prompt_template

    def test_noop_when_no_default_registered(self):
        r = KnowledgeRefiner(type="unknown-xyz", prompt_template="")
        assert kr._resolve_cfg(r).prompt_template == ""


# ---------------------------------------------------------------------------
# _task_filter
# ---------------------------------------------------------------------------

class TestTaskFilter:
    def _refiner(self) -> KnowledgeRefiner:
        return KnowledgeRefiner(type="task-filter", prompt_template="Review: {body}")

    def test_delete_on_delete_action(self, conn, monkeypatch):
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: '{"action": "delete", "reason": "stale"}')
        action, fields = kr._task_filter(conn, _make_cfg(), _row(), self._refiner())
        assert action == "delete"
        assert fields is None

    def test_skip_on_keep_action(self, conn, monkeypatch):
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: '{"action": "keep"}')
        action, _ = kr._task_filter(conn, _make_cfg(), _row(), self._refiner())
        assert action == "skip"

    def test_skip_on_llm_error(self, conn, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("network error")
        monkeypatch.setattr(ek, "call_llm", _boom)
        action, _ = kr._task_filter(conn, _make_cfg(), _row(), self._refiner())
        assert action == "skip"

    def test_skip_on_malformed_json(self, conn, monkeypatch):
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: "not json at all")
        action, _ = kr._task_filter(conn, _make_cfg(), _row(), self._refiner())
        assert action == "skip"


# ---------------------------------------------------------------------------
# _fact_check
# ---------------------------------------------------------------------------

class TestFactCheck:
    def _refiner(self) -> KnowledgeRefiner:
        return kr._resolve_cfg(KnowledgeRefiner(type="fact-check", prompt_template=""))

    def test_skip_when_no_snippets(self, conn, monkeypatch):
        monkeypatch.setattr(query_mod, "snippet_search", lambda *a, **kw: "")
        action, fields = kr._fact_check(conn, _make_cfg(), _row(), self._refiner())
        assert action == "skip"
        assert fields is None

    def test_skip_on_search_error(self, conn, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("embed failed")
        monkeypatch.setattr(query_mod, "snippet_search", _boom)
        action, _ = kr._fact_check(conn, _make_cfg(), _row(), self._refiner())
        assert action == "skip"

    def test_inconclusive_sets_fact_checked_at(self, conn, monkeypatch):
        monkeypatch.setattr(query_mod, "snippet_search", lambda *a, **kw: "def foo(): pass")
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: '{"verdict": "inconclusive"}')
        action, fields = kr._fact_check(conn, _make_cfg(), _row(), self._refiner())
        assert action == "update"
        assert "fact_checked_at" in fields
        assert "suppressed_at" not in fields

    def test_confirmed_sets_fact_checked_at(self, conn, monkeypatch):
        monkeypatch.setattr(query_mod, "snippet_search", lambda *a, **kw: "def foo(): pass")
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: '{"verdict": "confirmed"}')
        action, fields = kr._fact_check(conn, _make_cfg(), _row(), self._refiner())
        assert action == "update"
        assert "fact_checked_at" in fields
        assert "suppressed_at" not in fields

    def test_refuted_sets_suppressed_and_fact_checked_at(self, conn, monkeypatch):
        monkeypatch.setattr(query_mod, "snippet_search", lambda *a, **kw: "def foo(): pass")
        monkeypatch.setattr(ek, "call_llm", lambda *a, **kw: '{"verdict": "refuted"}')
        action, fields = kr._fact_check(conn, _make_cfg(), _row(), self._refiner())
        assert action == "update"
        assert "suppressed_at" in fields
        assert "fact_checked_at" in fields

    def test_derives_directory_from_pattern(self, conn, monkeypatch):
        captured = {}
        def _capture(c, cfg, text, directory, n=10):
            captured["directory"] = directory
            return ""
        monkeypatch.setattr(query_mod, "snippet_search", _capture)
        kr._fact_check(conn, _make_cfg(), _row(pattern="blerk_cmd/**"), self._refiner())
        assert captured["directory"] == "blerk_cmd"

    def test_wide_pattern_uses_dot_directory(self, conn, monkeypatch):
        captured = {}
        def _capture(c, cfg, text, directory, n=10):
            captured["directory"] = directory
            return ""
        monkeypatch.setattr(query_mod, "snippet_search", _capture)
        kr._fact_check(conn, _make_cfg(), _row(pattern="**"), self._refiner())
        assert captured["directory"] == "."


# ---------------------------------------------------------------------------
# _process_one
# ---------------------------------------------------------------------------

class TestProcessOne:
    def _mock_refiner(self, monkeypatch, action: str, fields=None) -> KnowledgeRefiner:
        monkeypatch.setitem(kr._REFINERS, "_mock", lambda conn, cfg, row, rcfg: (action, fields))
        return KnowledgeRefiner(type="_mock", enabled=True, prompt_template="x")

    def test_returns_false_when_queue_empty(self, conn):
        assert kr._process_one(conn, _make_cfg(), []) is False

    def test_returns_true_and_sets_refined_at(self, conn, monkeypatch):
        kid = _insert_knowledge(conn)
        r = self._mock_refiner(monkeypatch, "skip")
        assert kr._process_one(conn, _make_cfg(), [r]) is True
        row = conn.execute("SELECT refined_at FROM knowledge WHERE id=?", (kid,)).fetchone()
        assert row[0] is not None

    def test_delete_removes_row(self, conn, monkeypatch):
        kid = _insert_knowledge(conn)
        r = self._mock_refiner(monkeypatch, "delete")
        kr._process_one(conn, _make_cfg(), [r])
        assert conn.execute("SELECT id FROM knowledge WHERE id=?", (kid,)).fetchone() is None

    def test_update_writes_fields_and_refined_at(self, conn, monkeypatch):
        kid = _insert_knowledge(conn)
        r = self._mock_refiner(monkeypatch, "update", {"suppressed_at": 99999})
        kr._process_one(conn, _make_cfg(), [r])
        row = conn.execute("SELECT suppressed_at, refined_at FROM knowledge WHERE id=?", (kid,)).fetchone()
        assert row[0] == 99999
        assert row[1] is not None

    def test_already_refined_not_reprocessed(self, conn, monkeypatch):
        kid = _insert_knowledge(conn)
        conn.execute("UPDATE knowledge SET refined_at=unixepoch() WHERE id=?", (kid,))
        conn.commit()
        r = self._mock_refiner(monkeypatch, "delete")
        result = kr._process_one(conn, _make_cfg(), [r])
        assert result is False
        assert conn.execute("SELECT id FROM knowledge WHERE id=?", (kid,)).fetchone() is not None
