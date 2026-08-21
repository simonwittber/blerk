from __future__ import annotations

import hashlib
import struct
from io import StringIO
import sys

from blerk import db
from blerk_cmd.similar import similar


def _pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _insert_symbol(conn, file_id: int, name: str, line: int = 1) -> int:
    sid = int(conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?) RETURNING id",
        (file_id, name, "function", line, line + 5),
    ).lastrowid)
    return sid


def _insert_block(conn, sym_id: int, content: str | None = None) -> int:
    block_content = content if content is not None else f"def func_{sym_id}(): pass"
    content_hash = hashlib.sha256(block_content.encode("utf-8", errors="replace")).hexdigest()[:16]
    bid = int(conn.execute(
        "INSERT INTO code_blocks(symbol_id, block_index, content, content_hash, start_line, end_line)"
        " VALUES(?,?,?,?,?,?) RETURNING id",
        (sym_id, 0, block_content, content_hash, 1, 5),
    ).lastrowid)
    return bid


def _insert_embedding(conn, block_id: int, vector: list[float], model: str = "test-model") -> None:
    blob = _pack_vector(vector)
    row = conn.execute("SELECT content_hash FROM code_blocks WHERE id=?", (block_id,)).fetchone()
    content_hash = row[0]
    conn.execute(
        "INSERT INTO embeddings(content_hash, model, vector, embedded_at) VALUES(?,?,?,unixepoch())",
        (content_hash, model, blob),
    )


def test_no_blocks_in_directory(tmp_path):
    """Empty directory returns no results."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "nonexistent", threshold=0.15)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert "No indexed blocks found" in output
    finally:
        conn.close()


def test_no_matches_strict_threshold(tmp_path):
    """Threshold too strict finds no matches."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.py",))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.py", fid))

        sid1 = _insert_symbol(conn, fid, "func1", line=1)
        bid1 = _insert_block(conn, sid1)
        _insert_embedding(conn, bid1, [1.0, 0.0, 0.0])

        sid2 = _insert_symbol(conn, fid, "func2", line=10)
        bid2 = _insert_block(conn, sid2)
        _insert_embedding(conn, bid2, [0.0, 1.0, 0.0])  # orthogonal

        conn.commit()

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "", threshold=0.05)  # very strict
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert "No similar blocks found" in output
    finally:
        conn.close()


def test_similar_blocks_found(tmp_path):
    """Permissive threshold finds similar blocks."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.py",))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.py", fid))

        sid1 = _insert_symbol(conn, fid, "func1", line=1)
        bid1 = _insert_block(conn, sid1)
        _insert_embedding(conn, bid1, [1.0, 0.0, 0.0])

        sid2 = _insert_symbol(conn, fid, "func2", line=10)
        bid2 = _insert_block(conn, sid2)
        _insert_embedding(conn, bid2, [0.95, 0.05, 0.0])  # very close

        conn.commit()

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "", threshold=0.20)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert "func1" in output or "func2" in output
        assert "cluster" in output or "Found" in output
    finally:
        conn.close()


def test_extension_filtering(tmp_path):
    """Only blocks with matching extensions are scanned."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.py",))
        fid_py = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.py", fid_py))
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.go",))
        fid_go = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.go",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.go", fid_go))

        sid1 = _insert_symbol(conn, fid_py, "func_py", line=1)
        bid1 = _insert_block(conn, sid1)
        _insert_embedding(conn, bid1, [1.0, 0.0])

        sid2 = _insert_symbol(conn, fid_go, "func_go", line=1)
        bid2 = _insert_block(conn, sid2)
        _insert_embedding(conn, bid2, [0.99, 0.0])

        conn.commit()

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "", threshold=0.20, exts=[".py"])
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert "func_py" in output or "1 blocks" in output or "No similar blocks" in output
        assert "func_go" not in output
    finally:
        conn.close()


def test_multiple_embedding_models_selects_most_frequent(tmp_path):
    """When multiple models exist, select the one with most blocks."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.py",))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.py", fid))

        sid1 = _insert_symbol(conn, fid, "func1")
        bid1 = _insert_block(conn, sid1)
        _insert_embedding(conn, bid1, [1.0, 0.0], model="model-a")

        sid2 = _insert_symbol(conn, fid, "func2")
        bid2 = _insert_block(conn, sid2)
        _insert_embedding(conn, bid2, [0.99, 0.0], model="model-a")

        sid3 = _insert_symbol(conn, fid, "func3")
        bid3 = _insert_block(conn, sid3)
        _insert_embedding(conn, bid3, [0.5, 0.5], model="model-b")

        conn.commit()

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "", threshold=0.20)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert "model-a" in output or "2 blocks" in output
    finally:
        conn.close()


def test_union_find_groups_clusters(tmp_path):
    """Union-find correctly groups connected components."""
    db_path = str(tmp_path / "test.db")
    conn = db.open_db(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO files(hash, size) VALUES(?, 0)", ("code.py",))
        fid = int(conn.execute("SELECT id FROM files WHERE hash=?", ("code.py",)).fetchone()[0])
        conn.execute("INSERT INTO file_paths(path, mtime, file_id) VALUES(?, 0, ?)", ("code.py", fid))

        # Create a chain: A~B, B~C (so A,B,C form one cluster)
        sid_a = _insert_symbol(conn, fid, "funcA", line=1)
        bid_a = _insert_block(conn, sid_a)
        _insert_embedding(conn, bid_a, [1.0, 0.0])

        sid_b = _insert_symbol(conn, fid, "funcB", line=10)
        bid_b = _insert_block(conn, sid_b)
        _insert_embedding(conn, bid_b, [0.95, 0.0])

        sid_c = _insert_symbol(conn, fid, "funcC", line=20)
        bid_c = _insert_block(conn, sid_c)
        _insert_embedding(conn, bid_c, [0.90, 0.0])

        # Isolated D
        sid_d = _insert_symbol(conn, fid, "funcD", line=30)
        bid_d = _insert_block(conn, sid_d)
        _insert_embedding(conn, bid_d, [0.0, 1.0])

        conn.commit()

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            similar(conn, "", threshold=0.20)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        # Should have 1 cluster with A, B, C (D is isolated)
        assert "cluster 1" in output
        assert "funcA" in output
        assert "funcB" in output
        assert "funcC" in output
    finally:
        conn.close()
