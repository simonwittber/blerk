from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from blerk_cmd.util import normalize_dir

Violation = tuple[str, int, str, str, float]  # path, line, rule, display, score

RULES: list[Rule] = []


@dataclass
class Rule:
    name: str
    fn: Callable[..., list[Violation]]
    default: int    # threshold; -1 means disabled by default (opt-in)
    flag: str       # argparse long flag, e.g. "max-lines"
    help: str


def rule(default: int, flag: str, help: str):
    def decorator(fn: Callable) -> Callable:
        RULES.append(Rule(name=fn.__name__, fn=fn, default=default, flag=flag, help=help))
        return fn
    return decorator


def build_scope(conn, directory: str, excludes: list[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS _lint_files")
    parts: list[str] = []
    params: list = []
    if directory:
        norm = normalize_dir(directory).replace("\\", "/").rstrip("/")
        parts.append("(f.path LIKE ? OR f.path LIKE ?)")
        params += [f"%{norm}/%", f"%{norm}"]
    for pat in excludes:
        sql = pat.replace("\\", "/").replace("*", "%").replace("?", "_")
        parts.append("f.path NOT LIKE ?")
        params.append(sql)
    where = ("WHERE " + " AND ".join(parts)) if parts else ""
    conn.execute(
        f"CREATE TEMP TABLE _lint_files AS SELECT f.file_id AS file_id, f.path FROM file_paths f {where}",
        params,
    )
    conn.execute("CREATE INDEX _lint_files_fid ON _lint_files(file_id)")


@rule(default=40, flag="max-lines", help="max lines per function/method (default: 40)")
def long_function(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line, s.end_line
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.end_line IS NOT NULL
          AND (s.end_line - s.line) > ?
        ORDER BY f.path, s.line
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, line, "long_function", f"{name} ({end - line} lines)", (end - line) / t)
            for path, name, line, end in rows]


@rule(default=20, flag="max-symbols", help="max symbols per file (default: 20)")
def god_file(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, COUNT(*) AS sym_count
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        WHERE s.kind != 'heading'
        GROUP BY f.file_id
        HAVING sym_count > ?
        ORDER BY sym_count DESC
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, 1, "god_file", f"{count} symbols", count / t) for path, count in rows]


@rule(default=8, flag="max-callees", help="max callees per function/method (default: 8)")
def high_fan_out(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line,
               COUNT(DISTINCT r.callee_id) + COUNT(DISTINCT e.id) AS total_callees
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        LEFT JOIN symbol_refs r ON r.caller_id = s.id
        LEFT JOIN external_refs e ON e.caller_id = s.id
        WHERE s.kind IN ('function', 'method')
        GROUP BY s.id
        HAVING total_callees > ?
        ORDER BY f.path, s.line
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, line, "high_fan_out", f"{name} ({count} callees)", count / t)
            for path, name, line, count in rows]


@rule(default=4, flag="max-params", help="max parameters per function/method (default: 4)")
def too_many_params(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line, s.param_count
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.param_count > ?
        ORDER BY f.path, s.line
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, line, "too_many_params", f"{name} ({count} params)", count / t)
            for path, name, line, count in rows]


@rule(default=3, flag="max-nesting", help="max nesting depth (default: 3)")
def deep_nesting(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line, s.nesting_depth
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.nesting_depth > ?
        ORDER BY f.path, s.line
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, line, "deep_nesting", f"{name} (depth {depth})", depth / t)
            for path, name, line, depth in rows]


@rule(default=-1, flag="unused", help="flag functions/methods with no callers (opt-in)")
def unused_symbol(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        LEFT JOIN symbol_refs r ON r.callee_id = s.id
        WHERE s.kind IN ('function', 'method')
          AND r.callee_id IS NULL
        ORDER BY f.path, s.line
        """,
    ).fetchall()
    return [(path, line, "unused_symbol", name, 1.0) for path, name, line in rows]


@rule(default=3, flag="max-clone-distance", help="max SimHash Hamming distance for near-duplicate functions (default: 3, set -1 to disable)")
def duplicate_symbol(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    exact_rows = conn.execute(
        """
        WITH dup_hashes AS (
            SELECT fp.value
            FROM fingerprints fp
            JOIN symbols s ON s.id = fp.symbol_id
            JOIN _lint_files f ON f.file_id = s.file_id
            WHERE fp.kind = 'normhash'
            GROUP BY fp.value
            HAVING COUNT(DISTINCT s.file_id) > 1
        )
        SELECT f.path, s.line, s.name, fp.value
        FROM fingerprints fp
        JOIN symbols s ON s.id = fp.symbol_id
        JOIN _lint_files f ON f.file_id = s.file_id
        JOIN dup_hashes dh ON dh.value = fp.value
        WHERE fp.kind = 'normhash'
        ORDER BY fp.value, f.path, s.line
        """,
    ).fetchall()

    violations: list[Violation] = []

    # One violation per normhash group instead of one per file.
    if exact_rows:
        groups: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        for path, line, name, h in exact_rows:
            groups[h].append((path, line, name))
        for h, members in groups.items():
            members.sort()
            rep_path, rep_line, rep_name = members[0]
            count = len(members)
            display = f"exact clone: {rep_name} ({count} copies, hash {h[:8]})"
            violations.append((rep_path, rep_line, "exact_clone", display, 2.0))

    if threshold < 0:
        return violations

    sim_rows = conn.execute(
        """
        SELECT s.id, f.path, s.line, s.name, fp.value
        FROM fingerprints fp
        JOIN symbols s ON s.id = fp.symbol_id
        JOIN _lint_files f ON f.file_id = s.file_id
        WHERE fp.kind = 'simhash'
        ORDER BY fp.value
        """,
    ).fetchall()

    if not sim_rows:
        return violations

    hashes = [(sid, path, line, name, int(val, 16))
              for sid, path, line, name, val in sim_rows]
    n_bands = threshold + 1
    bits_per_band = 64 // n_bands
    band_mask = (1 << bits_per_band) - 1

    candidates: set[tuple[int, int]] = set()
    for b in range(n_bands):
        shift = b * bits_per_band
        mask = band_mask if b < n_bands - 1 else (1 << (64 - shift)) - 1
        buckets: dict[int, list[int]] = {}
        for i, (_, _, _, _, h) in enumerate(hashes):
            key = (h >> shift) & mask
            buckets.setdefault(key, []).append(i)
        for group in buckets.values():
            for x in range(len(group)):
                for y in range(x + 1, len(group)):
                    a, b_ = group[x], group[y]
                    if hashes[a][1] != hashes[b_][1]:
                        candidates.add((min(a, b_), max(a, b_)))

    # Verify candidates and build edge list for grouping.
    edges: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for a_idx, b_idx in candidates:
        sid_a, _, _, _, h_a = hashes[a_idx]
        sid_b, _, _, _, h_b = hashes[b_idx]
        dist = bin(h_a ^ h_b).count("1")
        if dist <= threshold:
            pair = (min(sid_a, sid_b), max(sid_a, sid_b))
            if pair not in seen:
                seen.add(pair)
                edges.append((sid_a, sid_b, dist))

    if not edges:
        return violations

    # Union-find: group near-clone pairs into connected components.
    all_nodes: set[int] = set()
    for sid_a, sid_b, _ in edges:
        all_nodes.add(sid_a)
        all_nodes.add(sid_b)

    parent: dict[int, int] = {sid: sid for sid in all_nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for sid_a, sid_b, _ in edges:
        ra, rb = find(sid_a), find(sid_b)
        if ra != rb:
            parent[ra] = rb

    hash_by_sid = {sid: (path, line, name) for sid, path, line, name, _ in hashes}
    comp_members: dict[int, list[tuple[str, int, str]]] = defaultdict(list)
    for sid in all_nodes:
        root = find(sid)
        path, line, name = hash_by_sid[sid]
        comp_members[root].append((path, line, name))

    comp_min_dist: dict[int, int] = {}
    for sid_a, sid_b, dist in edges:
        root = find(sid_a)
        if root not in comp_min_dist or dist < comp_min_dist[root]:
            comp_min_dist[root] = dist

    t = max(threshold, 1)
    for root, members in comp_members.items():
        members.sort()
        rep_path, rep_line, _ = members[0]
        min_d = comp_min_dist.get(root, threshold)
        n = len(members)
        score = 1.0 + (threshold - min_d) / t
        display = f"near-clone group: {n} symbols, closest distance {min_d}"
        violations.append((rep_path, rep_line, "near_clone", display, score))

    return violations


@rule(default=3, flag="dip-threshold", help="min dependents for a module to count as low-level in DIP hints (default: 3, set -1 to disable)")
def dip_violation(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    edges = conn.execute(
        """
        SELECT DISTINCT f1.path, f2.path
        FROM symbol_refs sr
        JOIN symbols s1 ON s1.id = sr.caller_id
        JOIN _lint_files f1 ON f1.file_id = s1.file_id
        JOIN symbols s2 ON s2.id = sr.callee_id
        JOIN _lint_files f2 ON f2.file_id = s2.file_id
        WHERE f1.path != f2.path
          AND s1.ext IS NOT NULL AND s1.ext = s2.ext
        """
    ).fetchall()
    if not edges:
        return []

    ns_map: dict[str, str] = {}
    for path, ns in conn.execute(
        """
        SELECT DISTINCT f.path, st.value
        FROM _lint_files f
        JOIN symbols s ON s.file_id = f.file_id
        JOIN symbol_tags st ON st.symbol_id = s.id AND st.key = 'namespace'
        WHERE f.path LIKE '%.cs'
        """
    ).fetchall():
        ns_map.setdefault(path, ns)

    def _module(path: str) -> str:
        p = path.replace("\\", "/")
        if p.endswith(".cs"):
            return ns_map.get(path, p)
        if p.endswith(".go"):
            idx = p.rfind("/")
            return p[:idx] if idx >= 0 else p
        return p

    module_edges: set[tuple[str, str]] = set()
    module_file: dict[str, str] = {}
    for importer_path, importee_path in edges:
        im, ie = _module(importer_path), _module(importee_path)
        if im == ie:
            continue
        module_edges.add((im, ie))
        module_file.setdefault(im, importer_path)

    inbound: dict[str, int] = {}
    for _, ie in module_edges:
        inbound[ie] = inbound.get(ie, 0) + 1

    t = max(threshold, 1)
    violations: list[Violation] = []
    for im, ie in sorted(module_edges):
        ie_inbound = inbound.get(ie, 0)
        if ie_inbound >= threshold and inbound.get(im, 0) < ie_inbound:
            display = f"may depend on lower-level module {ie} ({ie_inbound} dependents)"
            violations.append((module_file[im], 1, "dip_hint", display, ie_inbound / t))
    return violations


@rule(default=10, flag="max-deps", help="flag files that call into many other files (SRP hint, default: 10)")
def wide_module(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, COUNT(DISTINCT f2.id) AS dep_count
        FROM _lint_files f
        JOIN symbols s ON s.file_id = f.file_id
        JOIN symbol_refs sr ON sr.caller_id = s.id
        JOIN symbols callee ON callee.id = sr.callee_id
        JOIN files f2 ON f2.id = callee.file_id AND f2.id != f.file_id
        GROUP BY f.file_id
        HAVING dep_count > ?
        ORDER BY dep_count DESC
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, 1, "wide_module", f"{dep_count} file dependencies", dep_count / t)
            for path, dep_count in rows]


@rule(default=10, flag="max-methods", help="flag classes/structs/interfaces with more than N methods (ISP hint)")
def fat_class(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, c.name, c.line, COUNT(m.id) AS mc
        FROM symbols c
        JOIN _lint_files f ON f.file_id = c.file_id
        LEFT JOIN symbols m ON m.file_id = c.file_id
          AND m.kind = 'method'
          AND m.line > c.line
          AND (c.end_line IS NULL OR m.end_line <= c.end_line)
        WHERE c.kind IN ('class', 'struct', 'interface')
          AND c.end_line IS NOT NULL
        GROUP BY c.id
        HAVING mc > ?
        ORDER BY mc DESC
        """,
        (threshold,),
    ).fetchall()
    t = max(threshold, 1)
    return [(path, line, "fat_class", f"{name} ({mc} methods)", mc / t)
            for path, name, line, mc in rows]


@rule(default=-1, flag="statics", help="flag static symbols (opt-in)")
def static_symbol(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT f.path, s.name, s.line, s.kind
        FROM symbols s JOIN _lint_files f ON f.file_id = s.file_id
        JOIN symbol_tags st ON st.symbol_id = s.id AND st.key = 'is_static' AND st.value = 'true'
        ORDER BY f.path, s.line
        """,
    ).fetchall()
    return [(path, line, "static_symbol", f"{name} ({kind})", 1.0)
            for path, name, line, kind in rows]


def _union_find_components(nodes: set[int], edges: list[tuple[int, int]]) -> int:
    parent = {n: n for n in nodes}
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return len({find(n) for n in nodes})


@rule(default=5, flag="max-pkg-deps", help="max distinct packages a file may import from (SRP hint, default: 5)")
def wide_package(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT DISTINCT lf.path, f2.path
        FROM _lint_files lf
        JOIN symbols s1 ON s1.file_id = lf.file_id
        JOIN symbol_refs sr ON sr.caller_id = s1.id
        JOIN symbols s2 ON s2.id = sr.callee_id
        JOIN file_paths f2 ON f2.file_id = s2.file_id
        WHERE f2.file_id != lf.file_id
        """,
    ).fetchall()
    pkg_deps: dict[str, set[str]] = defaultdict(set)
    for caller, callee in rows:
        pkg_deps[caller].add(os.path.dirname(callee.replace("\\", "/")))
    t = max(threshold, 1)
    return [
        (p, 1, "wide_package", f"{len(pkgs)} packages", len(pkgs) / t)
        for p, pkgs in pkg_deps.items()
        if len(pkgs) > threshold
    ]


@rule(default=-1, flag="max-dep-spread", help="max dep-file/symbol ratio as integer percent (opt-in)")
def dep_spread(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT DISTINCT lf.path, f2.path
        FROM _lint_files lf
        JOIN symbols s1 ON s1.file_id = lf.file_id
        JOIN symbol_refs sr ON sr.caller_id = s1.id
        JOIN symbols s2 ON s2.id = sr.callee_id
        JOIN file_paths f2 ON f2.file_id = s2.file_id
        WHERE f2.file_id != lf.file_id
        """,
    ).fetchall()
    dep_files: dict[str, set[str]] = defaultdict(set)
    for caller, callee in rows:
        dep_files[caller].add(callee)

    sym_counts = dict(conn.execute(
        """
        SELECT lf.path, COUNT(s.id)
        FROM _lint_files lf
        JOIN symbols s ON s.file_id = lf.file_id
        GROUP BY lf.path
        """,
    ).fetchall())

    t = max(threshold, 1)
    violations: list[Violation] = []
    for p, deps in dep_files.items():
        total = max(sym_counts.get(p, 1), 1)
        ratio = int(len(deps) * 100 / total)
        if ratio > threshold:
            violations.append((p, 1, "dep_spread",
                f"{ratio}% spread ({len(deps)} deps, {total} symbols)", ratio / t))
    return violations


@rule(default=-1, flag="max-cohesion", help="flag classes with N+ disconnected method groups (LCOM, opt-in)")
def split_class(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    class_methods = conn.execute(
        """
        SELECT c.id, c.name, m.id, lf.path
        FROM _lint_files lf
        JOIN symbols c ON c.file_id = lf.file_id
          AND c.kind IN ('class', 'struct')
          AND c.end_line IS NOT NULL
        JOIN symbols m ON m.file_id = lf.file_id
          AND m.kind IN ('method', 'function')
          AND m.line > c.line
          AND m.end_line <= c.end_line
        """,
    ).fetchall()
    if not class_methods:
        return []

    class_info: dict[tuple[int, str], tuple[str, set[int]]] = {}
    for cid, cname, mid, path in class_methods:
        key = (cid, path)
        if key not in class_info:
            class_info[key] = (cname, set())
        class_info[key][1].add(mid)

    method_ids = {mid for _, (_, mids) in class_info.items() for mid in mids}
    placeholders = ",".join("?" * len(method_ids))
    ref_rows = conn.execute(
        f"""
        SELECT sr.caller_id, sr.callee_id
        FROM symbol_refs sr
        WHERE sr.caller_id IN ({placeholders})
          AND sr.callee_id IN ({placeholders})
        """,
        list(method_ids) + list(method_ids),
    ).fetchall()
    all_edges = list(ref_rows)

    t = max(threshold, 1)
    violations: list[Violation] = []
    for (_, path), (cname, mids) in class_info.items():
        if len(mids) < 2:
            continue
        class_edges = [(a, b) for a, b in all_edges if a in mids and b in mids]
        n = _union_find_components(mids, class_edges)
        if n >= threshold:
            violations.append((path, 1, "split_class",
                f"{cname} ({n} disconnected method groups)", n / t))
    return violations


@rule(default=-1, flag="abstraction-threshold", help="flag functions mixing high- and low-inbound deps (opt-in)")
def mixed_abstraction(conn, directory: str, threshold: int, excludes: list[str] = []) -> list[Violation]:
    rows = conn.execute(
        """
        WITH inbound AS (
            SELECT s2.file_id, COUNT(DISTINCT s1.file_id) AS caller_count
            FROM symbol_refs sr
            JOIN symbols s1 ON s1.id = sr.caller_id
            JOIN symbols s2 ON s2.id = sr.callee_id
            WHERE s1.file_id != s2.file_id
            GROUP BY s2.file_id
        )
        SELECT lf.path, s1.name, s1.line,
               COUNT(DISTINCT CASE WHEN COALESCE(i.caller_count, 0) >= 5 THEN f2.id END) AS high,
               COUNT(DISTINCT CASE WHEN COALESCE(i.caller_count, 0) <= 1 THEN f2.id END) AS low
        FROM _lint_files lf
        JOIN symbols s1 ON s1.file_id = lf.file_id
          AND s1.kind IN ('function', 'method')
        JOIN symbol_refs sr ON sr.caller_id = s1.id
        JOIN symbols s2 ON s2.id = sr.callee_id
        JOIN files f2 ON f2.id = s2.file_id
        LEFT JOIN inbound i ON i.file_id = f2.id
        WHERE f2.id != lf.file_id
        GROUP BY s1.id
        HAVING high >= ? AND low >= ?
        """,
        (threshold, threshold),
    ).fetchall()
    t = max(2 * threshold, 1)
    return [
        (path, line, "mixed_abstraction",
         f"{name} mixes {high} high-level + {low} low-level deps",
         (high + low) / t)
        for path, name, line, high, low in rows
    ]
