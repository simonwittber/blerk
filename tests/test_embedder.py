from __future__ import annotations

import struct

import httpx
import pytest

from blerk import db, embedding




def test_to_float32_blob_length():
    vec = [0.1, 0.2, 0.3, 0.4]
    blob = embedding.to_float32_blob(vec)
    assert len(blob) == len(vec) * 4


def test_to_float32_blob_round_trip():
    vec = [-1.5, 0.0, 0.25, 3.14159]
    blob = embedding.to_float32_blob(vec)
    got = struct.unpack(f"<{len(vec)}f", blob)
    for i, want in enumerate(vec):
        # Python floats are float64, so packing to <f truncates. Compare against the same round-trip.
        expected = struct.unpack("<f", struct.pack("<f", want))[0]
        assert got[i] == expected


def test_to_float32_blob_little_endian():
    vec = [0.5]
    blob = embedding.to_float32_blob(vec)
    assert len(blob) == 4
    want = struct.pack("<f", 0.5)
    assert blob == want


def test_to_float32_blob_empty():
    assert embedding.to_float32_blob([]) == b""


def _fake_post(monkeypatch, handler):
    monkeypatch.setattr(embedding.httpx, "post", handler)


def test_embed_posts_openai_shape(monkeypatch):
    seen = {}

    def handler(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    _fake_post(monkeypatch, handler)
    assert embedding.embed("http://host:11434", "nomic", "hello") == [1.0, 2.0]
    assert seen["url"] == "http://host:11434/v1/embeddings"
    assert seen["json"] == {"model": "nomic", "input": ["hello"]}
    assert seen["headers"] == {}


def test_embed_batch_sends_one_request_for_many_inputs(monkeypatch):
    calls = []

    def handler(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [2.0]},
            {"index": 2, "embedding": [3.0]},
        ]})

    _fake_post(monkeypatch, handler)
    got = embedding.embed_batch("http://host", "nomic", ["a", "b", "c"])
    assert got == [[1.0], [2.0], [3.0]]
    assert len(calls) == 1


def test_embed_batch_orders_by_index(monkeypatch):
    def handler(url, json=None, headers=None, timeout=None):
        return httpx.Response(200, json={"data": [
            {"index": 2, "embedding": [3.0]},
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [2.0]},
        ]})

    _fake_post(monkeypatch, handler)
    assert embedding.embed_batch("http://host", "m", ["a", "b", "c"]) == [[1.0], [2.0], [3.0]]


def test_embed_batch_empty_makes_no_request(monkeypatch):
    def handler(*a, **kw):
        raise AssertionError("should not post for an empty batch")

    _fake_post(monkeypatch, handler)
    assert embedding.embed_batch("http://host", "m", []) == []


def test_embed_sends_api_key_when_given(monkeypatch):
    seen = {}

    def handler(url, json=None, headers=None, timeout=None):
        seen.update(headers or {})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    _fake_post(monkeypatch, handler)
    embedding.embed("http://host", "m", "x", "sk-test")
    assert seen == {"Authorization": "Bearer sk-test"}


def test_embed_trailing_slash_does_not_double_up(monkeypatch):
    seen = {}

    def handler(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    _fake_post(monkeypatch, handler)
    embedding.embed("http://host:11434/", "m", "x")
    assert seen["url"] == "http://host:11434/v1/embeddings"


def test_embed_unreachable_endpoint_says_how_to_fix(monkeypatch):
    def handler(*a, **kw):
        raise httpx.ConnectError("connection refused")

    _fake_post(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("http://host:11434", "m", "x")
    msg = str(exc.value)
    assert "cannot reach the embedding endpoint at http://host:11434" in msg
    assert "ollama serve" in msg


def test_embed_error_status_is_reported(monkeypatch):
    def handler(*a, **kw):
        return httpx.Response(404, text='model "missing" not found')

    _fake_post(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("http://host", "missing", "x")
    assert "404" in str(exc.value)
    assert "not found" in str(exc.value)


def test_embed_empty_endpoint_names_the_setting(monkeypatch):
    def handler(*a, **kw):
        raise AssertionError("should not post without an endpoint")

    _fake_post(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("", "nomic", "hello")
    assert "embedder.endpoint is not set" in str(exc.value)


def test_embed_batch_rejects_short_response(monkeypatch):
    def handler(*a, **kw):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    _fake_post(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        embedding.embed_batch("http://host", "m", ["a", "b"])
    assert "1 vectors for 2 inputs" in str(exc.value)


def _build_text(name: str, description: str, snippet: str, max_embed_chars: int = 0) -> str:
    parts = [name]
    if description:
        parts.append(": ")
        parts.append(description)
    if snippet:
        parts.append("\n\n")
        parts.append(snippet)
    text = "".join(parts)
    if max_embed_chars > 0 and len(text) > max_embed_chars:
        text = text[:max_embed_chars]
    return text


def test_text_name_only():
    assert _build_text("foo", "", "") == "foo"


def test_text_name_and_description():
    assert _build_text("foo", "does stuff", "") == "foo: does stuff"


def test_text_name_and_snippet():
    assert _build_text("foo", "", "def foo(): pass") == "foo\n\ndef foo(): pass"


def test_text_all_three():
    assert _build_text("foo", "does stuff", "def foo(): pass") == "foo: does stuff\n\ndef foo(): pass"


def test_text_truncation():
    text = _build_text("foo", "x" * 100, "y" * 100, max_embed_chars=10)
    assert len(text) == 10
    assert text == "foo: xxxxx"


def test_text_no_truncation_when_max_is_zero():
    text = _build_text("foo", "bar", "baz", max_embed_chars=0)
    assert text == "foo: bar\n\nbaz"


def _insert_block(conn, tmp_path, sym_name: str = "foo") -> tuple[int, int]:
    p = str(tmp_path / "a.py")
    conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", (p,))
    fid = int(conn.execute("SELECT id FROM files WHERE hash=?", (p,)).fetchone()[0])
    conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (p, fid))
    sid = int(conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (fid, sym_name, "function", 1, 5),
    ).lastrowid)
    import hashlib
    content = "def foo(): pass"
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    bid = int(conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, content_hash, start_line, end_line)"
        " VALUES(?,?,?,?,?,?) RETURNING id",
        (sid, 0, content, content_hash, 1, 5),
    ).fetchone()[0])
    return sid, bid


def test_embedding_upsert_overwrites(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        _, bid = _insert_block(conn, tmp_path)
        content_hash = conn.execute("SELECT content_hash FROM code_blocks WHERE id=?", (bid,)).fetchone()[0]

        blob1 = embedding.to_float32_blob([1.0, 2.0, 3.0])
        blob2 = embedding.to_float32_blob([4.0, 5.0, 6.0])

        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) "
            "VALUES(?, ?, ?, unixepoch()) "
            "ON CONFLICT(content_hash, model) DO UPDATE SET "
            "vector = excluded.vector, embedded_at = excluded.embedded_at",
            (content_hash, "nomic", blob1),
        )
        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) "
            "VALUES(?, ?, ?, unixepoch()) "
            "ON CONFLICT(content_hash, model) DO UPDATE SET "
            "vector = excluded.vector, embedded_at = excluded.embedded_at",
            (content_hash, "nomic", blob2),
        )

        rows = conn.execute(
            "SELECT vector FROM embeddings WHERE content_hash=? AND model=?",
            (content_hash, "nomic"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == blob2

        count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE content_hash=?",
            (content_hash,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_embedding_upsert_different_model_creates_new_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        _, bid = _insert_block(conn, tmp_path)
        content_hash = conn.execute("SELECT content_hash FROM code_blocks WHERE id=?", (bid,)).fetchone()[0]

        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?, ?, ?, unixepoch())",
            (content_hash, "nomic", embedding.to_float32_blob([1.0])),
        )
        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?, ?, ?, unixepoch())",
            (content_hash, "other", embedding.to_float32_blob([2.0])),
        )

        count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE content_hash=?",
            (content_hash,),
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()
