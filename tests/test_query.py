from __future__ import annotations

import struct

import pytest

from blerk import db
from blerk_cmd import query
from blerk_cmd.query import QueryOptions, to_blob, truncate


def _seed_file(conn, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, hash) VALUES (?, 0, 0, '')",
        (path,),
    )
    return cur.lastrowid


def _seed_symbol(
    conn,
    file_id: int,
    name: str,
    kind: str = "function",
    line: int = 1,
    end_line: int = 5,
    description: str = "",
    snippet: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO symbols (file_id, name, kind, line, end_line, description) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, name, kind, line, end_line, description or None),
    )
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO code_blocks (symbol_id, block_index, content, start_line, end_line)"
        " VALUES (?, 0, ?, ?, ?)",
        (sid, snippet or "", line, end_line),
    )
    return sid


def _seed_embedding(conn, symbol_id: int, vec: list[float]) -> None:
    blob = struct.pack(f"<{len(vec)}f", *vec)
    row = conn.execute(
        "SELECT id FROM code_blocks WHERE symbol_id=? AND block_index=0", (symbol_id,)
    ).fetchone()
    block_id = row[0]
    conn.execute(
        "INSERT INTO embeddings (block_id, model, vector, embedded_at) VALUES (?, 'm', ?, 0)",
        (block_id, blob),
    )


def _open(tmp_path):
    return db.open_db(str(tmp_path / "test.db"))


def _write_config(tmp_path, db_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "secrets_file = \"\"\n"
        f"[db]\npath = \"{db_path.as_posix()}\"\n"
        "[embedder]\nendpoint = \"http://localhost:11434\"\nmodel = \"m\"\n"
    )
    return cfg


# --- helpers ---

def test_truncate_short():
    assert truncate("hello", 10) == "hello"


def test_truncate_exact():
    assert truncate("hello", 5) == "hello"


def test_truncate_long():
    assert truncate("abcdefghij", 6) == "abc..."


def test_to_blob_round_trip():
    vec = [1.0, -2.5, 3.25]
    blob = to_blob(vec)
    assert len(blob) == 12
    unpacked = struct.unpack("<3f", blob)
    assert unpacked == pytest.approx(tuple(vec))


# --- vector ranking ---

def test_vector_ranks_order(tmp_path):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/a.py")
    a = _seed_symbol(conn, file_id, "alpha", line=10)
    b = _seed_symbol(conn, file_id, "bravo", line=20)
    c = _seed_symbol(conn, file_id, "charlie", line=30)
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [0.9, 0.1, 0.0])
    _seed_embedding(conn, c, [0.0, 1.0, 0.0])

    ranks = query._vector_positions(conn, to_blob([1.0, 0.0, 0.0]), 10, QueryOptions())
    assert ranks[a] < ranks[b] < ranks[c]


# --- BM25 ranking ---

def test_bm25_ranks_match(tmp_path):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/a.py")
    a = _seed_symbol(conn, file_id, "debouncer", snippet="class Debouncer: pass")
    b = _seed_symbol(conn, file_id, "unrelated", snippet="def foo(): pass")

    ranks = query._bm25_symbol_positions(conn, "debouncer", 10, QueryOptions())
    assert a in ranks
    assert ranks.get(a, 999) < ranks.get(b, 999)


def test_bm25_ranks_empty_query(tmp_path):
    conn = _open(tmp_path)
    assert query._bm25_symbol_positions(conn, "", 10, QueryOptions()) == {}
    assert query._bm25_symbol_positions(conn, "   ", 10, QueryOptions()) == {}


# --- RRF fusion ---

def test_rrf_boosts_symbol_in_both_legs(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/a.py")

    # alpha: strong vector match, also matches BM25
    a = _seed_symbol(conn, file_id, "alpha_debounce", snippet="debounce timer reset")
    # bravo: weaker vector match, no BM25 match
    b = _seed_symbol(conn, file_id, "bravo", snippet="unrelated code here")

    _seed_embedding(conn, a, [0.9, 0.1, 0.0])
    _seed_embedding(conn, b, [1.0, 0.0, 0.0])  # bravo closer in vector space

    blob = to_blob([1.0, 0.0, 0.0])
    query.run_query(conn, blob, "debounce", QueryOptions(n=10))
    out = capsys.readouterr().out

    # alpha should appear first because RRF fuses both signals
    lines = out.splitlines()
    assert lines[0].startswith("function alpha_debounce")


# --- output format ---

def test_run_query_block_format(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/main.py")
    a = _seed_symbol(conn, file_id, "alpha", kind="function", line=10, end_line=25,
                     description="does alpha stuff", snippet="def alpha(): pass")
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "alpha", QueryOptions(n=10, verbose=True))
    out = capsys.readouterr().out

    assert "[1] function alpha" in out
    assert "src/main.py:10-25" in out
    assert "score:" in out
    assert "does alpha stuff" in out
    assert "  def alpha(): pass" in out


def test_run_query_no_description_or_snippet(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/main.py")
    a = _seed_symbol(conn, file_id, "alpha", line=1, end_line=1)
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "alpha", QueryOptions(n=10))
    out = capsys.readouterr().out

    assert "description:" not in out
    assert "snippet:" not in out


def test_run_query_empty_db_prints_nothing(tmp_path, capsys):
    conn = _open(tmp_path)
    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "anything", QueryOptions(n=10))
    assert capsys.readouterr().out == ""


# --- ext filter ---

def test_ext_filter_excludes_other_lang(tmp_path, capsys):
    conn = _open(tmp_path)
    py_file = _seed_file(conn, "src/a.py")
    cs_file = _seed_file(conn, "src/b.cs")
    a = _seed_symbol(conn, py_file, "alpha_py")
    b = _seed_symbol(conn, cs_file, "alpha_cs")
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "alpha", QueryOptions(n=10, exts=[".py"]))
    out = capsys.readouterr().out

    assert "alpha_py" in out
    assert "alpha_cs" not in out


def test_ext_filter_multi(tmp_path, capsys):
    conn = _open(tmp_path)
    py_file = _seed_file(conn, "src/a.py")
    cs_file = _seed_file(conn, "src/b.cs")
    go_file = _seed_file(conn, "src/c.go")
    a = _seed_symbol(conn, py_file, "alpha_py")
    b = _seed_symbol(conn, cs_file, "alpha_cs")
    c = _seed_symbol(conn, go_file, "alpha_go")
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [1.0, 0.0, 0.0])
    _seed_embedding(conn, c, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "alpha", QueryOptions(n=10, exts=[".py", ".cs"]))
    out = capsys.readouterr().out

    assert "alpha_py" in out
    assert "alpha_cs" in out
    assert "alpha_go" not in out


def test_heading_excluded_by_default(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "docs/README.md")
    a = _seed_symbol(conn, file_id, "Introduction", kind="heading")
    b = _seed_symbol(conn, file_id, "get_thing", kind="function")
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "Introduction", QueryOptions(n=10))
    out = capsys.readouterr().out

    assert "Introduction" not in out
    assert "get_thing" in out


def test_heading_included_when_md_ext(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "docs/README.md")
    a = _seed_symbol(conn, file_id, "Introduction", kind="heading")
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "Introduction", QueryOptions(n=10, exts=[".md"]))
    out = capsys.readouterr().out

    assert "Introduction" in out


# --- refs ---

def test_print_refs_calls_and_calledby(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/a.py")
    caller = _seed_symbol(conn, file_id, "caller_fn", line=1)
    target = _seed_symbol(conn, file_id, "target_fn", line=10)
    callee = _seed_symbol(conn, file_id, "callee_fn", line=20)

    conn.execute("INSERT INTO symbol_refs (caller_id, callee_id) VALUES (?, ?)", (target, callee))
    conn.execute("INSERT INTO symbol_refs (caller_id, callee_id) VALUES (?, ?)", (caller, target))

    query.print_refs(conn, target)
    out = capsys.readouterr().out

    assert "calls: callee_fn" in out
    assert "calledby: caller_fn" in out


def test_run_query_with_refs(tmp_path, capsys):
    conn = _open(tmp_path)
    file_id = _seed_file(conn, "src/main.py")
    a = _seed_symbol(conn, file_id, "alpha", line=1)
    b = _seed_symbol(conn, file_id, "bravo", line=2)
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    conn.execute("INSERT INTO symbol_refs (caller_id, callee_id) VALUES (?, ?)", (a, b))

    query.run_query(conn, to_blob([1.0, 0.0, 0.0]), "alpha", QueryOptions(n=10, refs=True))
    out = capsys.readouterr().out

    assert "[1] function alpha" in out
    assert "calls: bravo" in out


# --- main ---

def test_main_empty_db_prints_nothing(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "empty.db"
    db.open_db(str(db_path)).close()
    cfg_path = _write_config(tmp_path, db_path)
    monkeypatch.setattr(query, "embed", lambda *a: [1.0, 0.0, 0.0])

    assert query.main(["--config", str(cfg_path), "hello", "."]) == 0
    assert capsys.readouterr().out == ""


def test_main_end_to_end(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "e2e.db"
    conn = db.open_db(str(db_path))
    file_id = _seed_file(conn, "src/main.py")
    a = _seed_symbol(conn, file_id, "alpha", line=10)
    b = _seed_symbol(conn, file_id, "bravo", line=20)
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [0.0, 1.0, 0.0])
    conn.close()

    cfg_path = _write_config(tmp_path, db_path)
    monkeypatch.setattr(query, "embed", lambda *a: [1.0, 0.0, 0.0])

    assert query.main(["--config", str(cfg_path), "-n", "1", "--verbose", "alpha", "src"]) == 0
    out = capsys.readouterr().out
    assert "[1] function alpha" in out
    assert "bravo" not in out


def test_main_ext_flag(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "e2e.db"
    conn = db.open_db(str(db_path))
    py_file = _seed_file(conn, "src/main.py")
    cs_file = _seed_file(conn, "src/main.cs")
    a = _seed_symbol(conn, py_file, "alpha_py", line=1)
    b = _seed_symbol(conn, cs_file, "alpha_cs", line=1)
    _seed_embedding(conn, a, [1.0, 0.0, 0.0])
    _seed_embedding(conn, b, [1.0, 0.0, 0.0])
    conn.close()

    cfg_path = _write_config(tmp_path, db_path)
    monkeypatch.setattr(query, "embed", lambda *a: [1.0, 0.0, 0.0])

    assert query.main(["--config", str(cfg_path), "--ext", ".py", "alpha", "src"]) == 0
    out = capsys.readouterr().out
    assert "alpha_py" in out
    assert "alpha_cs" not in out
