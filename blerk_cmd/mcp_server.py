from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

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


def _call(name: str, args: dict) -> str:
    if name == "search":
        n = max(1, min(int(args.get("n", 10)), 50))
        cmd = ["query", args["query"], "-n", str(n)]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        cmd.append(args["directory"])
        return _run(*cmd) or "No results found."

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
        from blerk_cmd.summary import summary
        cfg = config.load(cfg_path)
        cwd = normalize_dir(os.getcwd())
        watched = any(
            cwd.startswith(normalize_dir(f).rstrip("/"))
            for f in cfg.watch.folders
        )
        if not watched:
            return ""
        return "This project is indexed by blerk. Run blerk summary for file counts, recent changes, and findings."
    except Exception:
        return ""


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args()

    from blerk import config as _config
    cfg_path = args.config or _config.default_path()

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
