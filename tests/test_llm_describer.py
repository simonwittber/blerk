from __future__ import annotations

import httpx
import pytest

from blerk import db

from blerk_cmd import llm_describer
from blerk_cmd.llm_describer import SymbolInfo, build_prompt, describe


def _install_transport(handler):
    original = llm_describer._client
    llm_describer._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return original


def _restore_client(original):
    llm_describer._client.close()
    llm_describer._client = original


def test_describe_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    original = _install_transport(handler)
    try:
        result = describe("http://api.local", "test-model", "secret", "hello")
        assert result == "hi"
    finally:
        _restore_client(original)


def test_describe_no_auth_header_when_key_empty():
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    original = _install_transport(handler)
    try:
        describe("http://api.local", "m", "", "prompt")
        assert "authorization" not in {k.lower() for k in seen_headers}
    finally:
        _restore_client(original)


def test_describe_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    original = _install_transport(handler)
    try:
        with pytest.raises(RuntimeError) as exc:
            describe("http://api.local", "m", "", "prompt")
        assert "500" in str(exc.value)
        assert "server error" in str(exc.value)
    finally:
        _restore_client(original)


def test_describe_raises_on_empty_choices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    original = _install_transport(handler)
    try:
        with pytest.raises(RuntimeError) as exc:
            describe("http://api.local", "m", "", "prompt")
        assert "empty" in str(exc.value)
    finally:
        _restore_client(original)


def test_build_prompt_neutralizes_braces_in_substituted_values(tmp_path):
    source = tmp_path / "src.py"
    source.write_text("def foo():\n    pass\n")

    sym = SymbolInfo(
        id=1,
        name="foo{evil}",
        kind="fun{bar}",
        path=str(source),
        line=1,
        end_line=2,
    )
    template = 'kind={kind} name={name} path={path}\n{context}'
    out = build_prompt(sym, template, 16000)

    assert "{evil}" not in out
    assert "(evil)" in out
    assert "{bar}" not in out
    assert "fun(bar)" in out
    assert "===== DESCRIBE THIS SYMBOL =====" in out


def test_build_prompt_source_unavailable_when_path_missing(tmp_path):
    missing = tmp_path / "does_not_exist.py"
    sym = SymbolInfo(
        id=1,
        name="foo",
        kind="function",
        path=str(missing),
        line=1,
        end_line=2,
    )
    out = build_prompt(sym, "{context}", 100)
    assert "(source unavailable:" in out


def test_build_prompt_preserves_template_literal_braces(tmp_path):
    source = tmp_path / "src.py"
    source.write_text("x = 1\n")
    sym = SymbolInfo(id=1, name="x", kind="var", path=str(source), line=1, end_line=1)
    # Template braces that aren't the four supported placeholders must survive verbatim.
    template = "prefix {unknown} {name} suffix"
    out = build_prompt(sym, template, 100)
    assert "{unknown}" in out
    assert " x " in out


def test_description_write_enqueues_embedding(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
            (str(tmp_path / "a.py"), 0, "h"),
        )
        fid = int(cur.lastrowid)

        cur = conn.execute(
            "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
            (fid, "foo", "function", 1, 5),
        )
        sid = int(cur.lastrowid)

        # symbols_after_insert already enqueues one embedding row.
        conn.execute("DELETE FROM embedding_queue WHERE symbol_id=?", (sid,))

        conn.execute(
            "UPDATE symbols SET description=?, described_at=unixepoch() WHERE id=?",
            ("a description", sid),
        )

        row = conn.execute(
            "SELECT COUNT(*) FROM embedding_queue WHERE symbol_id=?",
            (sid,),
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()
