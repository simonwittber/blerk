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


def test_embed_unknown_backend():
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("unknown", "http://api.local", "nomic", "hello")
    assert "unknown embedding backend" in str(exc.value)


def test_embed_sentence_transformers_import_error(monkeypatch):
    def mock_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("test error")
        return __import__(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", mock_import)
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("sentence-transformers", "", "all-MiniLM", "hello")
    assert "sentence-transformers not installed" in str(exc.value)


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
    fid = int(conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (str(tmp_path / "a.py"), 0, "h"),
    ).lastrowid)
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
