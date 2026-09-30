from __future__ import annotations

import struct

from blerk import config, db, embedding


def test_embedding_model_switch_requires_reindex(tmp_path):
    """Document that switching embedding models leaves stale embeddings.

    Embeddings are keyed by (content_hash, model), so vectors from the old model stay in the table
    with their own dimensions and are simply never matched again.
    Run `blerk reindex --all` to build vectors for the new model.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)

    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("test.py",))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", ("test.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("test.py", fid))
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

        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?, ?, ?, unixepoch())",
            (content_hash, "nomic-embed-text", struct.pack("<768f", *([0.1] * 768))),
        )
        conn.execute(
            "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?, ?, ?, unixepoch())",
            (content_hash, "mxbai-embed-large", struct.pack("<1024f", *([0.2] * 1024))),
        )
        conn.commit()

        # Both coexist, keyed by model, and a search filters to the configured one.
        models = {r[0] for r in conn.execute(
            "SELECT model FROM embeddings WHERE content_hash = ?", (content_hash,)
        ).fetchall()}
        assert models == {"nomic-embed-text", "mxbai-embed-large"}

        row = conn.execute(
            "SELECT vector FROM embeddings WHERE content_hash=? AND model=?",
            (content_hash, "nomic-embed-text"),
        ).fetchone()
        assert len(row[0]) == 768 * 4
    finally:
        conn.close()


def test_embedder_config_has_no_local_model_settings():
    """blerk never loads model weights, so there is nothing to configure beyond the endpoint and model."""
    fields = set(config.Embedder.__dataclass_fields__)
    assert "backend" not in fields
    assert "device" not in fields
    assert "cache_dir" not in fields
    assert {"endpoint", "model", "api_key"} <= fields


def test_embedding_module_exports_are_consistent():
    assert callable(embedding.embed)
    assert callable(embedding.embed_batch)
    assert callable(embedding.to_float32_blob)
