from __future__ import annotations

import sqlite3

from blerk import db
from blerk_cmd import embedder


def _seed(conn, path="/repo/a.py", desc=None):
    conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES('h1', 0)")
    fid = conn.execute("SELECT id FROM files WHERE hash='h1'").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", (path, fid))
    sid = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line, description) VALUES(?,?,?,?,?,?)",
        (fid, "spawn_wave", "function", 1, 9, desc),
    ).lastrowid
    bid = conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, content_hash, start_line, end_line)"
        " VALUES(?,0,'def spawn_wave(): pass','ch1',1,9)", (sid,),
    ).lastrowid
    conn.commit()
    return sid, bid


def _store(conn, bid, model="m"):
    ch, _name, _path, text = embedder.build_embed_text(conn, bid, model)
    conn.execute(
        "INSERT INTO embeddings(content_hash, model, vector, embedded_at, input_hash) VALUES(?,?,?,0,?)",
        (ch, model, b"\x00" * 8, embedder.input_hash(text)),
    )
    conn.commit()


def test_unchanged_text_is_skipped(conn):
    _sid, bid = _seed(conn)
    _store(conn, bid)
    assert embedder.build_embed_text(conn, bid, "m") is None


def test_new_description_forces_a_rebuild(conn):
    """The original bug: the vector was keyed on code alone, so a later description never reached it."""
    sid, bid = _seed(conn)
    _store(conn, bid)
    conn.execute("UPDATE symbols SET description='Spawns the next wave of enemies.' WHERE id=?", (sid,))
    built = embedder.build_embed_text(conn, bid, "m")
    assert built is not None, "a changed description must cause a re-embed"
    assert "Spawns the next wave" in built[3]


def test_legacy_rows_without_input_hash_are_rebuilt(conn):
    _sid, bid = _seed(conn)
    conn.execute("INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES('ch1','m',?,0)", (b"\x00" * 8,))
    conn.commit()
    assert embedder.build_embed_text(conn, bid, "m") is not None


def test_other_model_does_not_count(conn):
    _sid, bid = _seed(conn)
    _store(conn, bid, model="old-model")
    assert embedder.build_embed_text(conn, bid, "new-model") is not None


def test_description_change_requeues_the_symbols_blocks(conn):
    sid, bid = _seed(conn)
    conn.execute("DELETE FROM code_block_embed_queue")
    conn.execute("UPDATE symbols SET description='Spawns enemies.' WHERE id=?", (sid,))
    rows = conn.execute("SELECT block_id FROM code_block_embed_queue").fetchall()
    assert [r[0] for r in rows] == [bid]


def test_unchanged_description_does_not_requeue(conn):
    sid, _bid = _seed(conn, desc="Spawns enemies.")
    conn.execute("DELETE FROM code_block_embed_queue")
    conn.execute("UPDATE symbols SET description='Spawns enemies.' WHERE id=?", (sid,))
    assert conn.execute("SELECT COUNT(*) FROM code_block_embed_queue").fetchone()[0] == 0


def test_v21_database_gains_input_hash(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL,
            model TEXT NOT NULL, vector BLOB NOT NULL, embedded_at INTEGER NOT NULL);
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (21);
        INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES ('x', 'm', x'00', 0);
    """)
    raw.close()
    conn = db.open_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
    assert "input_hash" in cols
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 22
    assert conn.execute("SELECT input_hash FROM embeddings").fetchone()[0] is None, "existing rows must be rebuilt"
