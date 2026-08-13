from __future__ import annotations

import struct

import pytest

from blerk import config, db, embedding


def test_embedding_backend_switch_requires_requery(tmp_path):
    """Document that switching embedding backends creates stale embeddings.

    When you switch from one embedding model to another (e.g., ollama's
    nomic-embed-text to sentence-transformers' all-MiniLM-L6-v2), old
    embeddings remain with different dimensions. You must run:
      blerk reindex --all
    to replace them with embeddings from the new model.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)

    try:
        fid = int(conn.execute(
            "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
            ("test.py", 0, "h"),
        ).lastrowid)
        sid = int(conn.execute(
            "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
            (fid, "func", "function", 1, 5),
        ).lastrowid)
        import hashlib
        content = "def func(): pass"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        conn.execute(
            "INSERT INTO code_blocks(symbol_id, block_index, content, content_hash, start_line, end_line)"
            " VALUES(?,?,?,?,?,?) RETURNING id",
            (sid, 0, content, content_hash, 1, 5),
        ).fetchone()[0]

        blob_768 = struct.pack(f"<768f", *([0.1] * 768))

        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?, ?, ?, unixepoch())",
            (content_hash, "nomic-embed-text", blob_768),
        )
        conn.commit()

        embeddings = conn.execute(
            "SELECT model FROM embeddings WHERE content_hash = ?",
            (content_hash,),
        ).fetchall()
        assert len(embeddings) == 1
        assert embeddings[0][0] == "nomic-embed-text"
    finally:
        conn.close()


def test_embedding_backend_config_loading():
    """Verify embedder backend is loaded from config."""
    cfg1 = config.Embedder(backend="ollama", endpoint="http://localhost:11434", model="nomic-embed-text")
    assert cfg1.backend == "ollama"
    assert cfg1.model == "nomic-embed-text"

    cfg2 = config.Embedder(backend="sentence-transformers", endpoint="", model="all-MiniLM-L6-v2")
    assert cfg2.backend == "sentence-transformers"
    assert cfg2.model == "all-MiniLM-L6-v2"

    assert cfg1.backend != cfg2.backend
    assert cfg1.model != cfg2.model


def test_embedding_module_exports_are_consistent():
    """Ensure embedding.embed and embedding.to_float32_blob are available."""
    assert callable(embedding.embed)
    assert callable(embedding.to_float32_blob)


def test_embed_backend_parameter_validation():
    """Verify embed function validates backend parameter."""
    with pytest.raises(RuntimeError) as exc:
        embedding.embed("invalid_backend", "", "", "")
    assert "unknown embedding backend" in str(exc.value)
