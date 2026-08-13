from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3 as _sqlite3

_seen_hint_ids: set[int] = set()
_conn: "_sqlite3.Connection | None" = None

_TOOLS = [
    {
        "name": "search",
        "description": "Search indexed source code symbols using natural language.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "directory": {"type": "string"},
                "file_extensions": {"type": "array", "items": {"type": "string"}},
                "n": {"type": "integer"},
            },
            "required": ["query", "directory"],
        },
    },
    {
        "name": "browse",
        "description": "List indexed source files. Set symbols=true for an indented symbol tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "file_extensions": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "boolean"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "detail",
        "description": "Get description, snippet, callers, and callees for a named symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "deps",
        "description": "Show the file-level dependency graph as an adjacency list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "hint_store",
        "description": "Save a hint tied to a file-glob pattern. Wide patterns (** or *) create project-level hints that always surface.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "description": "short tag e.g. 'path-normalization'"},
                "pattern": {"type": "string", "description": "fnmatch glob e.g. 'src/indexing/**' or '**'"},
                "body":    {"type": "string", "description": "one or two sentence actionable note"},
                "source":  {"type": "string", "enum": ["auto", "explicit"]},
            },
            "required": ["concept", "pattern", "body"],
        },
    },
    {
        "name": "hint_session_reset",
        "description": "Reset the seen-hint set so all hints can be re-injected. Called automatically after context compaction.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "show",
        "description": "Show source code for an indexed file or symbol, read directly from the original source file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "file path or exact symbol name"},
                "file": {"type": "string", "description": "restrict symbol lookup to a file path substring"},
                "lines": {"type": "integer", "description": "maximum number of source lines to display"},
            },
            "required": ["target"],
        },
    },
]


_PATH_RE = re.compile(r"\s{2,}(\S+):\d+-\d+")


def _pattern_matches(path: str, pattern: str) -> bool:
    from blerk_cmd.util import normalize_dir
    pattern = normalize_dir(pattern)
    parts = path.split("/")
    for i in range(len(parts)):
        if fnmatch.fnmatch("/".join(parts[i:]), pattern):
            return True
    return False


def _hints_for_paths(paths: list[str]) -> str:
    if _conn is None:
        return ""
    hint_rows = _conn.execute(
        "SELECT id, concept, body, pattern FROM hints ORDER BY created_at"
    ).fetchall()
    matched = []
    for id_, concept, body, pattern in hint_rows:
        if id_ in _seen_hint_ids:
            continue
        is_wide = pattern in ("*", "**", "**/*")
        if is_wide or any(_pattern_matches(p, pattern) for p in paths):
            matched.append(f"[Hint: {concept}] {body}")
            _seen_hint_ids.add(id_)
    if not matched:
        return ""
    return "\nRelevant hints:\n" + "\n".join(matched)


def _run(*args: str) -> str:
    result = subprocess.run(
        ["blerk"] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout
    if result.returncode != 0 and not output:
        return result.stderr.strip() or f"blerk exited with code {result.returncode}"
    return output


def _call(name: str, args: dict) -> str:  # noqa: C901
    if name == "hint_store":
        if _conn is None:
            return "Hint store unavailable: database not open."
        _conn.execute(
            "INSERT INTO hints(concept, pattern, body, source) VALUES (?,?,?,?)",
            (args["concept"], args["pattern"], args["body"], args.get("source", "explicit")),
        )
        _conn.commit()
        return f"Hint stored: [{args['concept']}] {args['body']}"

    if name == "hint_session_reset":
        _seen_hint_ids.clear()
        return "Hint session reset."

    # existing tools below
    if name == "search":
        n = max(1, min(int(args.get("n", 10)), 50))
        cmd = ["query", args["query"], "-n", str(n)]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        cmd.append(args["directory"])
        output = _run(*cmd) or "No results found."
        from blerk_cmd.util import normalize_dir
        paths = [normalize_dir(p) for p in _PATH_RE.findall(output)]
        return output + _hints_for_paths(paths)

    if name == "browse":
        cmd = ["browse"]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        if args.get("symbols"):
            cmd.append("--symbols")
        cmd.append(args["directory"])
        return _run(*cmd) or "No indexed files found."

    if name == "detail":
        cmd = ["detail", args["name"]]
        if args.get("file_path"):
            cmd += ["--file", args["file_path"]]
        return _run(*cmd)

    if name == "deps":
        cmd = ["deps", args["directory"]]
        return _run(*cmd) or "No dependencies found."

    if name == "show":
        cmd = ["show", args["target"]]
        if args.get("file"):
            cmd += ["--file", args["file"]]
        if args.get("lines"):
            cmd += ["--lines", str(args["lines"])]
        return _run(*cmd)

    return f"Unknown tool: {name}"


def _build_instructions(cfg_path: str) -> str:
    try:
        from blerk import config
        from blerk_cmd.util import normalize_dir
        cfg = config.load(cfg_path)
        cwd = normalize_dir(os.getcwd())
        watched = any(
            cwd.startswith(normalize_dir(f).rstrip("/"))
            for f in cfg.watch.folders
        )
        if not watched:
            return ""
        return cfg.hints.instructions
    except Exception:
        return ""


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    global _conn
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args()

    from blerk import config as _config, db as _db
    cfg_path = args.config or _config.default_path()
    try:
        _cfg = _config.load(cfg_path)
        _conn = _db.open_db(_cfg.db.path)
    except Exception:
        _conn = None

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            result: dict = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "blerk", "version": "0.3.0"},
            }
            instructions = _build_instructions(cfg_path)
            if instructions:
                result["instructions"] = instructions
            _send({"jsonrpc": "2.0", "id": req_id, "result": result})

        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}})

        elif method == "tools/call":
            try:
                text = _call(params.get("name", ""), params.get("arguments") or {})
            except Exception as e:
                _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}})
                continue
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": text}]
            }})

        elif req_id is not None:
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
