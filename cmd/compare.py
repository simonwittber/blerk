#!/usr/bin/env python3
"""
Blerk vs traditional tools comparison script.

Run this against an indexed codebase to measure context efficiency and accuracy.
Each task runs once with blerk CLI tools and once with Python-native traditional tools.
The summary table shows output line count (context cost proxy) and elapsed time.

Usage:
    python cmd/compare.py ~/git/module-games
    python cmd/compare.py ~/git/module-games --package-dir Packages --ext .cs
    python cmd/compare.py ~/git/my-py-project --ext .py

Add or modify tasks in build_tasks() below.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Run:
    label: str
    lines: int
    ms: int
    output: str
    error: bool = False


def run_blerk(cmd: str, timeout: int = 30, head: int = 0) -> Run:
    """Run a blerk CLI command using the system shell."""
    t0 = time.monotonic()
    error = False
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout
        if result.returncode != 0:
            output += result.stderr
            error = True
    except subprocess.TimeoutExpired:
        output = f"[TIMEOUT after {timeout}s]"
        error = True
    if head:
        lines = output.splitlines()
        output = "\n".join(lines[:head])
        if len(lines) > head:
            output += f"\n... ({len(lines) - head} more lines)"
    ms = int((time.monotonic() - t0) * 1000)
    non_blank = len([l for l in output.splitlines() if l.strip()])
    return Run(label=cmd, lines=non_blank, ms=ms, output=output, error=error)


def run_trad(fn: Callable[[], str], timeout: int = 30) -> Run:
    """Run a Python-native traditional tool function."""
    t0 = time.monotonic()
    error = False
    try:
        output = fn()
    except Exception as e:
        output = f"[ERROR: {e}]"
        error = True
    ms = int((time.monotonic() - t0) * 1000)
    non_blank = len([l for l in output.splitlines() if l.strip()])
    return Run(label="<python>", lines=non_blank, ms=ms, output=output, error=error)


# ---------------------------------------------------------------------------
# Traditional tool implementations (Python-native, cross-platform)
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"Library", "node_modules", ".git", "Temp"}


def _skip(p: Path) -> bool:
    return any(part in _SKIP_DIRS for part in p.parts)


def trad_package_list(pkg_path: str) -> str:
    """List packages by scanning package.json files."""
    import json
    lines: list[str] = []
    for p in sorted(Path(pkg_path).rglob("package.json")):
        if _skip(p):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            name = data.get("name", p.parent.name)
            desc = data.get("description", "")
            lines.append(f"{name}  {desc}".rstrip())
        except Exception:
            lines.append(p.parent.name)
    return "\n".join(lines[:30])


def trad_grep_files(target: str, patterns: list[str], ext: str) -> str:
    """Find files that contain any of the given regex patterns."""
    rx = re.compile("|".join(patterns))
    hits: list[str] = []
    for p in sorted(Path(target).rglob(f"*{ext}")):
        if _skip(p):
            continue
        try:
            if rx.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(p))
        except Exception:
            pass
    return "\n".join(hits[:20])


def trad_grep_lines(target: str, pattern: str, ext: str) -> str:
    """Return matching lines with file:line context."""
    rx = re.compile(pattern)
    hits: list[str] = []
    for p in sorted(Path(target).rglob(f"*{ext}")):
        if _skip(p):
            continue
        try:
            for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if rx.search(line):
                    hits.append(f"{p}:{i}: {line.strip()}")
        except Exception:
            pass
    return "\n".join(hits[:40])


def trad_file_sizes(target: str, ext: str) -> str:
    """List files sorted by line count (largest first)."""
    sizes: list[tuple[int, Path]] = []
    for p in Path(target).rglob(f"*{ext}"):
        if _skip(p):
            continue
        try:
            sizes.append((p.read_text(encoding="utf-8", errors="replace").count("\n"), p))
        except Exception:
            pass
    sizes.sort(reverse=True)
    return "\n".join(f"{n:6d}  {p}" for n, p in sizes[:20])


def trad_using_namespaces(target: str, ext: str) -> str:
    """Collect unique using/import statements."""
    rx = re.compile(r"^\s*using\s+\S")
    seen: set[str] = set()
    for p in Path(target).rglob(f"*{ext}"):
        if _skip(p):
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if rx.match(line):
                    seen.add(line.strip())
        except Exception:
            pass
    return "\n".join(sorted(seen)[:40])


# ---------------------------------------------------------------------------
# Task definition
# ---------------------------------------------------------------------------

@dataclass
class Task:
    name: str
    description: str
    blerk_cmd: str
    trad_fn: Callable[[], str]
    blerk_head: int = 0


def build_tasks(target: str, pkg_dir: str, ext: str) -> list[Task]:
    pkg_path = f"{target}/{pkg_dir}" if pkg_dir else target
    # normalise to forward slashes for blerk CLI
    target = target.replace("\\", "/")
    pkg_path = pkg_path.replace("\\", "/")

    return [
        Task(
            name="T1: Orientation",
            description="List packages and their purpose",
            blerk_cmd=f'blerk browse --dir "{pkg_path}" --ext .json',
            trad_fn=lambda: trad_package_list(pkg_path),
        ),
        Task(
            name="T2: Concept search (events)",
            description="Find event system without knowing its name",
            blerk_cmd=(
                f'blerk query --dir "{target}" --ext {ext}'
                ' "game event system publish subscribe" -n 10'
            ),
            trad_fn=lambda: trad_grep_files(
                target,
                ["GameEvent", "IGameEvent", "EventBus", "EventChannel", "ScriptableEvent"],
                ext,
            ),
        ),
        Task(
            name="T3: Concept search (BT)",
            description="Find a behaviour tree system without knowing which package owns it",
            blerk_cmd=(
                f'blerk query --dir "{target}" --ext {ext}'
                ' "behaviour tree node sequence selector" -n 10'
            ),
            trad_fn=lambda: trad_grep_files(
                target,
                ["BehaviourTree", "BTNode", "INode", r"\bSelector\b", r"\bSequence\b"],
                ext,
            ),
        ),
        Task(
            name="T4: Caller lookup",
            description="Find all callers of SpawnEnemy",
            blerk_cmd="blerk detail SpawnEnemy",
            trad_fn=lambda: trad_grep_lines(target, r"\bSpawnEnemy\b", ext),
        ),
        Task(
            name="T5: Dependency graph",
            description="Show file-level deps for the packages directory",
            blerk_cmd=f'blerk deps --dir "{pkg_path}"',
            trad_fn=lambda: trad_using_namespaces(pkg_path, ext),
            blerk_head=40,
        ),
        Task(
            name="T6: Lint",
            description="Surface large files and over-complex functions",
            blerk_cmd=f'blerk lint --dir "{pkg_path}"',
            trad_fn=lambda: trad_file_sizes(pkg_path, ext),
            blerk_head=40,
        ),
    ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_run_detail(r: Run, max_lines: int = 30) -> None:
    status = " [ERROR]" if r.error else ""
    print(f"  {r.lines} lines  {r.ms}ms{status}")
    shown = r.output.splitlines()[:max_lines]
    for line in shown:
        print(f"    {line}")
    remaining = r.lines - len(shown)
    if remaining > 0:
        print(f"    ... ({remaining} more lines)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", help="Root directory of the indexed codebase")
    parser.add_argument(
        "--package-dir",
        default="Packages",
        help="Sub-directory containing packages (default: Packages). Pass '' to skip.",
    )
    parser.add_argument("--ext", default=".cs", help="File extension filter (default: .cs)")
    parser.add_argument(
        "--timeout", type=int, default=30, help="Per-command timeout in seconds"
    )
    args = parser.parse_args()

    target = args.target.rstrip("/\\")
    tasks = build_tasks(target, args.package_dir, args.ext)

    print(f"Target : {target}")
    print(f"Ext    : {args.ext}")
    print(f"Tasks  : {len(tasks)}")
    print()

    results: list[tuple[Task, Run, Run]] = []
    for task in tasks:
        print(f"Running {task.name} ...", flush=True)
        br = run_blerk(task.blerk_cmd, args.timeout, head=task.blerk_head)
        tr = run_trad(task.trad_fn, args.timeout)
        results.append((task, br, tr))

    W = 30
    print()
    print("=" * 88)
    print(
        f"{'Task':<{W}} {'Blerk lines':>12} {'ms':>6}   {'Trad lines':>12} {'ms':>6}   {'ratio':>6}"
    )
    print("=" * 88)
    for task, br, tr in results:
        ratio = br.lines / max(tr.lines, 1)
        arrow = (
            "<<" if ratio < 0.5
            else "<" if ratio < 1
            else "~" if ratio < 1.5
            else "> " if ratio < 3
            else ">>"
        )
        berr = "*" if br.error else ""
        terr = "*" if tr.error else ""
        print(
            f"{task.name:<{W}}"
            f" {br.lines:>11}{berr:<1} {br.ms:>6}"
            f"   {tr.lines:>11}{terr:<1} {tr.ms:>6}"
            f"   {ratio:>5.1f}x {arrow}"
        )
    print("=" * 88)
    print("ratio < 1 = blerk used less context | >> = blerk used much more | * = error")
    print()

    for task, br, tr in results:
        print(f"## {task.name}")
        print(f"   {task.description}")
        print()
        print(f"  [blerk]  $ {task.blerk_cmd}")
        print_run_detail(br)
        print()
        print("  [trad]   (python-native)")
        print_run_detail(tr)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
