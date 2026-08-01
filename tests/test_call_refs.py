"""
Tests that expose critical failures in cross-file caller/callee ref tracking.

Three failure modes:
1. C# instance method calls (_obj.Method()) not extracted by treesitter.
2. Processing-order bug: caller indexed before callee -> ref stored in external_refs
   and never promoted to symbol_refs when callee arrives.
3. Rescan invalidation: rescanning the callee deletes its symbols (CASCADE), which
   deletes symbol_refs rows pointing to it; they are never rebuilt.
"""
from __future__ import annotations

import pytest

from blerk import config, db
from blerk.symbols.treesitter_extractor import Extractor
from blerk.symbols.types import CallRef, Symbol
from blerk_cmd.symbolizer import process_symbols


# ---------------------------------------------------------------------------
# Helpers shared with test_symbolizer.py
# ---------------------------------------------------------------------------


def _cfg() -> config.Config:
    cfg = config.defaults()
    cfg.symbolizer.min_describe_lines = 0
    return cfg


def _insert_file(conn, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, mtime, hash) VALUES(?,?,?)",
        (path, 0, "h"),
    )
    return int(cur.lastrowid)



# ---------------------------------------------------------------------------
# Failure 1: treesitter C# call extraction for instance method calls
# ---------------------------------------------------------------------------

def test_cs_direct_call_extracted(write_temp):
    """Sanity check: direct calls like Foo() are captured."""
    src = """\
public class A {
    public void Caller() { Callee(); }
    public void Callee() { }
}
"""
    path = write_temp("a.cs", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "A.Caller" and r.callee_name == "A.Callee" for r in refs), \
        f"Expected A.Caller->A.Callee in refs, got: {refs}"


def test_cs_instance_method_call_extracted(write_temp):
    """
    _obj.SpawnEnemy() must produce a CallRef(caller='Update', callee='SpawnEnemy').
    This is the pattern used throughout the module-games codebase.
    Fails if the CS_CALL query or node_at_line/find_body_child drops member-access calls.
    """
    src = """\
public class EnemySpawner {
    private RenderableSimulation _simulation;

    private void Update() {
        _simulation.SpawnEnemy(0, 0);
    }
}

public class RenderableSimulation {
    public void SpawnEnemy(int a, int b) { }
}
"""
    path = write_temp("spawner.cs", src)
    _, refs = Extractor().extract(path)
    callee_names = {r.callee_name for r in refs}
    assert "RenderableSimulation.SpawnEnemy" in callee_names, \
        f"Expected RenderableSimulation.SpawnEnemy in callee names, got: {callee_names}"
    assert any(r.caller_name == "EnemySpawner.Update" and r.callee_name == "RenderableSimulation.SpawnEnemy" for r in refs), \
        f"Expected EnemySpawner.Update->RenderableSimulation.SpawnEnemy, got: {refs}"


def test_cs_chained_method_call_extracted(write_temp):
    """obj.Inner.Method() - the leaf method name must be captured."""
    src = """\
public class Ship {
    private Weapon _weapon;
    public void Fire() {
        _weapon.Shoot(Vector2.zero);
    }
}
public class Weapon {
    public void Shoot(Vector2 pos) { }
}
"""
    path = write_temp("ship.cs", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "Ship.Fire" and r.callee_name == "Weapon.Shoot" for r in refs), \
        f"Expected Ship.Fire->Weapon.Shoot, got: {refs}"


# ---------------------------------------------------------------------------
# Failure 2: processing-order bug
# ---------------------------------------------------------------------------

def test_cross_file_ref_callee_first(conn):
    """When callee is indexed before caller, symbol_refs must be populated."""
    cfg = _cfg()

    fid_callee = _insert_file(conn, "callee.cs")
    process_symbols(
        conn, cfg, db.QueueRow(1, fid_callee), "callee.cs",
        [Symbol("SpawnEnemy", "method", 1, 5, "void SpawnEnemy(){}")],
        [],
    )

    fid_caller = _insert_file(conn, "caller.cs")
    process_symbols(
        conn, cfg, db.QueueRow(2, fid_caller), "caller.cs",
        [Symbol("Update", "method", 1, 5, "void Update(){}")],
        [CallRef("Update", "SpawnEnemy")],
    )

    ref_count = conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0]
    assert ref_count == 1, "callee-first ordering must produce a symbol_refs row"


def test_cross_file_ref_caller_first(conn):
    """
    When caller is indexed BEFORE the callee exists in the DB, the ref must
    still appear in symbol_refs once the callee is later indexed.

    Currently FAILS: the ref is placed in external_refs and never promoted.
    """
    cfg = _cfg()

    # Caller file indexed first - callee not yet in DB.
    fid_caller = _insert_file(conn, "caller.cs")
    process_symbols(
        conn, cfg, db.QueueRow(1, fid_caller), "caller.cs",
        [Symbol("Update", "method", 1, 5, "void Update(){}")],
        [CallRef("Update", "SpawnEnemy")],
    )

    # Ref should have gone to external_refs.
    ext_count = conn.execute("SELECT COUNT(*) FROM external_refs WHERE callee_name='SpawnEnemy'").fetchone()[0]
    assert ext_count == 1, "unresolved ref must be recorded in external_refs"

    # Now callee file is indexed.
    fid_callee = _insert_file(conn, "callee.cs")
    process_symbols(
        conn, cfg, db.QueueRow(2, fid_callee), "callee.cs",
        [Symbol("SpawnEnemy", "method", 1, 5, "void SpawnEnemy(){}")],
        [],
    )

    ref_count = conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0]
    assert ref_count == 1, \
        "external_refs must be promoted to symbol_refs when callee is later indexed"


# ---------------------------------------------------------------------------
# Failure 3: rescan invalidation
# ---------------------------------------------------------------------------

def test_rescan_caller_preserves_refs(conn):
    """
    Re-indexing the CALLER file must rebuild the ref to an already-indexed callee.
    Currently FAILS if the old caller symbols are deleted (CASCADE removes symbol_refs)
    and the new insert does not re-create the ref because cross-file lookup only
    runs when refs are present in the extraction result.
    """
    cfg = _cfg()

    fid_callee = _insert_file(conn, "callee.cs")
    process_symbols(
        conn, cfg, db.QueueRow(1, fid_callee), "callee.cs",
        [Symbol("SpawnEnemy", "method", 1, 5, "void SpawnEnemy(){}")],
        [],
    )

    fid_caller = _insert_file(conn, "caller.cs")
    process_symbols(
        conn, cfg, db.QueueRow(2, fid_caller), "caller.cs",
        [Symbol("Update", "method", 1, 5, "void Update(){}")],
        [CallRef("Update", "SpawnEnemy")],
    )

    assert conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0] == 1

    # Rescan caller (same refs, same symbols - simulates blerk rescan).
    process_symbols(
        conn, cfg, db.QueueRow(3, fid_caller), "caller.cs",
        [Symbol("Update", "method", 1, 5, "void Update(){}")],
        [CallRef("Update", "SpawnEnemy")],
    )

    ref_count = conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0]
    assert ref_count == 1, \
        "rescanning caller must rebuild the symbol_refs row"


def test_rescan_callee_preserves_refs(conn):
    """
    Re-indexing the CALLEE file deletes its symbols (CASCADE removes symbol_refs
    rows where callee_id pointed to those symbols) and re-inserts with new IDs.
    The ref must be rebuilt.

    Currently FAILS: the old symbol_refs row is CASCADE-deleted and nothing
    recreates it because the caller file is not re-processed.
    """
    cfg = _cfg()

    fid_callee = _insert_file(conn, "callee.cs")
    process_symbols(
        conn, cfg, db.QueueRow(1, fid_callee), "callee.cs",
        [Symbol("SpawnEnemy", "method", 1, 5, "void SpawnEnemy(){}")],
        [],
    )

    fid_caller = _insert_file(conn, "caller.cs")
    process_symbols(
        conn, cfg, db.QueueRow(2, fid_caller), "caller.cs",
        [Symbol("Update", "method", 1, 5, "void Update(){}")],
        [CallRef("Update", "SpawnEnemy")],
    )

    assert conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0] == 1

    # Rescan callee - its symbols get new IDs.
    process_symbols(
        conn, cfg, db.QueueRow(3, fid_callee), "callee.cs",
        [Symbol("SpawnEnemy", "method", 1, 5, "void SpawnEnemy(){}")],
        [],
    )

    ref_count = conn.execute("SELECT COUNT(*) FROM symbol_refs").fetchone()[0]
    assert ref_count == 1, \
        "rescanning callee must preserve or rebuild the symbol_refs row pointing to it"
