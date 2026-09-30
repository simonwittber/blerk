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


def test_run_skips_already_described_block(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    p = str(tmp_path / "a.py")
    conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", (p,))
    fid = int(conn.execute("SELECT id FROM files WHERE hash=?", (p,)).fetchone()[0])
    conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (p, fid))
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (fid, "foo", "function", 1, 5),
    )
    sid = int(cur.lastrowid)
    bid = int(conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, start_line, end_line, description)"
        " VALUES(?,?,?,?,?,?) RETURNING id",
        (sid, 0, "def foo(): pass", 1, 5, "already described"),
    ).fetchone()[0])
    conn.execute("INSERT INTO code_block_describe_queue(block_id) VALUES (?)", (bid,))
    conn.close()

    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "new desc"}}]})

    import threading
    from blerk import config as blerk_config
    from blerk_cmd.llm_describer import run as describer_run

    original = _install_transport(handler)
    try:
        cfg = blerk_config.defaults()
        cfg.db.path = db_path
        llm = blerk_config.defaults().llm[0]
        shutdown = threading.Event()

        def _stop():
            import time as _time
            _time.sleep(0.05)
            shutdown.set()

        t = threading.Thread(target=_stop, daemon=True)
        t.start()
        describer_run(cfg, llm, shutdown)
        t.join()
    finally:
        _restore_client(original)

    assert call_count[0] == 0


def test_code_block_insert_enqueues_embed_and_describe(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        p = str(tmp_path / "a.py")
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", (p,))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", (p,)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (p, fid))

        cur = conn.execute(
            "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
            (fid, "foo", "function", 1, 5),
        )
        sid = int(cur.lastrowid)

        conn.execute(
            "INSERT INTO code_blocks(symbol_id, block_index, content, start_line, end_line)"
            " VALUES(?,?,?,?,?)",
            (sid, 0, "def foo(): pass", 1, 5),
        )

        embed_count = conn.execute(
            "SELECT COUNT(*) FROM code_block_embed_queue"
        ).fetchone()[0]
        describe_count = conn.execute(
            "SELECT COUNT(*) FROM code_block_describe_queue"
        ).fetchone()[0]
        assert embed_count == 1
        assert describe_count == 1
    finally:
        conn.close()


def test_describe_omits_thinking_kwarg_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    original = _install_transport(handler)
    try:
        describe("http://api.local", "m", "", "p")
        assert "chat_template_kwargs" not in seen
    finally:
        _restore_client(original)


def test_describe_think_false_disables_thinking():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    original = _install_transport(handler)
    try:
        describe("http://api.local", "m", "", "p", think=False)
        assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        _restore_client(original)


def test_describe_trailing_slash_does_not_double_up():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    original = _install_transport(handler)
    try:
        describe("http://api.local/", "m", "", "p")
        assert seen["url"] == "http://api.local/v1/chat/completions"
    finally:
        _restore_client(original)


# --- tidy_description ---

from blerk_cmd.llm_describer import tidy_description


def test_tidy_flattens_newlines():
    assert tidy_description("one\n\ntwo\nthree") == "one two three"


def test_tidy_strips_bullets_and_headings():
    raw = "## Summary\n- does a thing\n- and another\n1. numbered too"
    assert tidy_description(raw) == "Summary does a thing and another numbered too"


def test_tidy_unwraps_bold_labels():
    assert tidy_description("**Purpose:** spawns enemies") == "Purpose: spawns enemies"


def test_tidy_keeps_short_text_untouched():
    s = "Increments the kill counter and updates the HUD text."
    assert tidy_description(s) == s


def test_tidy_caps_at_a_sentence_boundary():
    s = "First sentence here. Second sentence here. " + "x" * 500
    out = tidy_description(s, 60)
    assert out == "First sentence here. Second sentence here."
    assert len(out) <= 60


def test_tidy_falls_back_to_word_boundary_when_no_sentence_end():
    out = tidy_description("alpha beta gamma delta epsilon zeta eta theta", 20)
    assert len(out) <= 23
    assert out.endswith("...")
    assert " " in out and not out.startswith(" ")


def test_tidy_handles_empty_and_zero_cap():
    assert tidy_description("") == ""
    assert tidy_description("keep all of this", 0) == "keep all of this"
