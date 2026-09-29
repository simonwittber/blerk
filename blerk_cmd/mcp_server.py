from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3 as _sqlite3

_seen_knowledge_ids: set[int] = set()
_conn: "_sqlite3.Connection | None" = None
_root: str = ""

_DIRECTORY_ARG = {
    "type": "string",
    "description": (
        "Directory to restrict to, written relative to the index root exactly as you would for reading a file"
        " (for example 'blerk_cmd' or 'src/indexing'). An absolute path also works."
        " Omit it to search the whole index root. Results come back as root-relative paths."
    ),
}

_FILE_ARG = {
    "type": "string",
    "description": (
        "File to restrict to, relative to the index root (for example 'blerk_cmd/query.py')."
        " An absolute path, or a bare file name, also works."
    ),
}

_TOOLS = [
    {
        "name": "search",
        "description": "Search indexed source code symbols using natural language.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "directory": _DIRECTORY_ARG,
                "file_extensions": {"type": "array", "items": {"type": "string"}},
                "n": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "browse",
        "description": (
            "List indexed source files. Set symbols=true for an indented symbol tree."
            " Call with no arguments to list the index roots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": _DIRECTORY_ARG,
                "file_extensions": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "boolean"},
                "roots": {"type": "boolean", "description": "List the index roots instead of any files."},
            },
            "required": [],
        },
    },
    {
        "name": "detail",
        "description": "Get description, snippet, callers, and callees for a named symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "file": _FILE_ARG,
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
                "directory": _DIRECTORY_ARG,
            },
            "required": [],
        },
    },
    {
        "name": "knowledge_store",
        "description": "Save a knowledge item tied to a file-glob pattern. Wide patterns (** or *) create project-level items that always surface.",
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
        "name": "knowledge_session_reset",
        "description": "Reset the seen-knowledge set so all knowledge items can be re-injected. Called automatically after context compaction.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "show",
        "description": "Show source code for an indexed file or symbol, read directly from the original source file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "file path or exact symbol name"},
                "file": _FILE_ARG,
                "lines": {"type": "integer", "description": "maximum number of source lines to display"},
            },
            "required": ["target"],
        },
    },
]


_PATH_RE = re.compile(r"\s{2,}(\S+):\d+-\d+")


def _pattern_matches(path: str, pattern: str) -> bool:
    from blerk_cmd.util import to_slash
    pattern = to_slash(pattern)
    parts = path.split("/")
    for i in range(len(parts)):
        if fnmatch.fnmatch("/".join(parts[i:]), pattern):
            return True
    return False


def _hints_for_paths(paths: list[str]) -> str:
    if _conn is None:
        return ""
    hint_rows = _conn.execute(
        "SELECT id, concept, body, pattern, created_at FROM knowledge"
        " WHERE suppressed_at IS NULL ORDER BY created_at"
    ).fetchall()
    now = int(_time.time())
    matched = []
    ids_to_update = []
    for id_, concept, body, pattern, created_at in hint_rows:
        if id_ in _seen_knowledge_ids:
            continue
        is_wide = pattern in ("*", "**", "**/*")
        if is_wide or any(_pattern_matches(p, pattern) for p in paths):
            age_days = (now - (created_at or now)) // 86400
            age_str = "today" if age_days == 0 else f"{age_days}d old"
            matched.append(f"{body} ({age_str})")
            _seen_knowledge_ids.add(id_)
            ids_to_update.append(id_)
    if ids_to_update:
        _conn.executemany(
            "UPDATE knowledge SET surfaced_count = surfaced_count + 1 WHERE id=?",
            [(i,) for i in ids_to_update],
        )
        _conn.commit()
    if not matched:
        return ""
    return "\nHints:\n" + "\n".join(matched)


def _directory_args(args: dict) -> list[str]:
    """Return the directory as a positional argument list, empty when the caller did not scope the call."""
    directory = (args.get("directory") or "").strip()
    return [directory] if directory else []


def _empty(what: str, args: dict) -> str:
    """Say what scope came up empty, so the caller can tell a wrong path from an empty directory."""
    directory = (args.get("directory") or "").strip()
    where = directory or (_root or "the index")
    line = f"No {what} in {where}."
    if _root:
        line += f" Index root: {_root}."
    line += " Paths are relative to the index root; call browse with roots=true to list the roots."
    return line


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
    if name == "knowledge_store":
        if _conn is None:
            return "Knowledge store unavailable: database not open."
        _conn.execute(
            "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
            (args["concept"], args["pattern"], args["body"], args.get("source", "explicit")),
        )
        _conn.commit()
        return f"Knowledge stored: [{args['concept']}] {args['body']}"

    if name == "knowledge_session_reset":
        _seen_knowledge_ids.clear()
        return "Hint session reset."

    # existing tools below
    if name == "search":
        n = max(1, min(int(args.get("n", 10)), 50))
        cmd = ["query", args["query"], "-n", str(n)]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        cmd += _directory_args(args)
        output = _run(*cmd) or _empty("results", args)
        from blerk_cmd.util import to_slash
        paths = [to_slash(p) for p in _PATH_RE.findall(output)]
        return output + _hints_for_paths(paths)

    if name == "browse":
        if args.get("roots"):
            return _run("browse", "--roots")
        cmd = ["browse"]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        if args.get("symbols"):
            cmd.append("--symbols")
        cmd += _directory_args(args)
        return _run(*cmd) or _empty("indexed files", args)

    if name == "detail":
        cmd = ["detail", args["name"]]
        if args.get("file"):
            cmd += ["--file", args["file"]]
        return _run(*cmd)

    if name == "deps":
        return _run("deps", *_directory_args(args)) or _empty("dependencies", args)

    if name == "show":
        cmd = ["show", args["target"]]
        if args.get("file"):
            cmd += ["--file", args["file"]]
        if args.get("lines"):
            cmd += ["--lines", str(args["lines"])]
        return _run(*cmd)

    return f"Unknown tool: {name}"


def _build_instructions(cfg_path: str) -> str:
    """Return the server instructions, naming the index root this session is working inside."""
    global _root
    try:
        from blerk import config
        from blerk_cmd.util import index_root
        cfg = config.load(cfg_path)
        _root = index_root(cfg)
        if not _root:
            return ""
        return (
            f"{cfg.knowledge.instructions}\n"
            f"Index root: {_root}\n"
            "Paths in blerk arguments and results are relative to that root, "
            "the same form you use for reading and editing files. "
            "The directory argument is optional and defaults to the whole root."
        ).strip()
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
