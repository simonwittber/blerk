from __future__ import annotations

import io
import json
import sys

import pytest

import blerk_cmd.mcp_server as mcp_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drive(monkeypatch, requests: list[dict]) -> list[dict]:
    collected: list[dict] = []
    stdin_data = "\n".join(json.dumps(r) for r in requests) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
    monkeypatch.setattr(mcp_mod, "_send", lambda obj: collected.append(obj))
    mcp_mod.main()
    return collected


# ---------------------------------------------------------------------------
# _call() unit tests — _run is monkeypatched
# ---------------------------------------------------------------------------

class TestCall:
    def test_search_calls_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("search", {"query": "foo bar", "directory": "."})
        assert calls[0][0] == "query"
        assert "foo bar" in calls[0]

    def test_search_n_clamped_to_50(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("search", {"query": "x", "n": 999, "directory": "."})
        args = list(calls[0])
        assert int(args[args.index("-n") + 1]) == 50

    def test_search_n_minimum_1(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("search", {"query": "x", "n": -5, "directory": "."})
        args = list(calls[0])
        assert int(args[args.index("-n") + 1]) == 1

    def test_search_empty_result_fallback(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "")
        assert mcp_mod._call("search", {"query": "x", "directory": "."}) == "No results found."

    def test_search_directory_passed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("search", {"query": "x", "directory": "src/core"})
        assert "--dir" not in calls[0]
        assert "src/core" in calls[0]

    def test_search_extensions_passed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("search", {"query": "x", "file_extensions": [".py", ".cs"], "directory": "."})
        args = list(calls[0])
        assert args.count("--ext") == 2

    def test_browse_fallback(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "")
        assert mcp_mod._call("browse", {"directory": "."}) == "No indexed files found."

    def test_browse_symbols_flag_added(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("browse", {"symbols": True, "directory": "."})
        assert "--symbols" in calls[0]

    def test_browse_symbols_flag_omitted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("browse", {"symbols": False, "directory": "."})
        assert "--symbols" not in calls[0]

    def test_detail_passes_name(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("detail", {"name": "my_fn"})
        assert "detail" in calls[0]
        assert "my_fn" in calls[0]

    def test_detail_file_path_passed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("detail", {"name": "my_fn", "file_path": "src/a.py"})
        assert "--file" in calls[0]
        assert "src/a.py" in calls[0]

    def test_deps_fallback(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "")
        assert mcp_mod._call("deps", {"directory": "."}) == "No dependencies found."

    def test_deps_directory_passed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: calls.append(a) or "ok")
        mcp_mod._call("deps", {"directory": "src"})
        assert "--dir" not in calls[0]
        assert "src" in calls[0]

    def test_unknown_tool_returns_error_message(self, monkeypatch):
        result = mcp_mod._call("does_not_exist", {})
        assert "Unknown tool" in result


# ---------------------------------------------------------------------------
# main() loop tests
# ---------------------------------------------------------------------------

class TestMain:
    def test_initialize_response(self, monkeypatch):
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ])
        assert len(responses) == 1
        r = responses[0]
        assert r["id"] == 1
        assert r["result"]["protocolVersion"] == "2024-11-05"
        assert r["result"]["serverInfo"]["name"] == "blerk"

    def test_tools_list_returns_all_tools(self, monkeypatch):
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        ])
        names = {t["name"] for t in responses[0]["result"]["tools"]}
        assert {"search", "browse", "detail", "deps"} <= names

    def test_tools_call_returns_text_content(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "search result")
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "foo", "directory": "."}}}
        ])
        content = responses[0]["result"]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "search result"

    def test_tools_call_fallback_when_run_empty(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "")
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "browse", "arguments": {"directory": "."}}}
        ])
        assert responses[0]["result"]["content"][0]["text"] == "No indexed files found."

    def test_unknown_method_with_id_returns_error(self, monkeypatch):
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 5, "method": "unknown/method", "params": {}}
        ])
        assert len(responses) == 1
        assert responses[0]["error"]["code"] == -32601

    def test_notification_no_id_produces_no_response(self, monkeypatch):
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ])
        assert len(responses) == 0

    def test_parse_error_returns_error_response(self, monkeypatch):
        collected: list[dict] = []
        monkeypatch.setattr(sys, "stdin", io.StringIO("not valid json\n"))
        monkeypatch.setattr(mcp_mod, "_send", lambda obj: collected.append(obj))
        mcp_mod.main()
        assert len(collected) == 1
        assert collected[0]["error"]["code"] == -32700

    def test_multiple_requests_handled_in_sequence(self, monkeypatch):
        monkeypatch.setattr(mcp_mod, "_run", lambda *a: "ok")
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "x"}}},
        ])
        assert len(responses) == 3
        assert [r["id"] for r in responses] == [1, 2, 3]

    def test_tools_call_exception_returns_error(self, monkeypatch):
        def _bad_call(name, args):
            raise ValueError("boom")
        monkeypatch.setattr(mcp_mod, "_call", _bad_call)
        responses = _drive(monkeypatch, [
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "x"}}}
        ])
        assert responses[0]["error"]["code"] == -32603
        assert "boom" in responses[0]["error"]["message"]

    def test_blank_lines_ignored(self, monkeypatch):
        collected: list[dict] = []
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n\n\n"))
        monkeypatch.setattr(mcp_mod, "_send", lambda obj: collected.append(obj))
        mcp_mod.main()
        assert len(collected) == 0
