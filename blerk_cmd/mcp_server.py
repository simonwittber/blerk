from __future__ import annotations

import json
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
            "required": ["query"],
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
        },
    },
    {
        "name": "lint",
        "description": "Lint code using the blerk index. Flags large files and complex functions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "max_lines": {"type": "integer"},
                "max_symbols": {"type": "integer"},
                "max_callees": {"type": "integer"},
                "max_params": {"type": "integer"},
                "max_nesting": {"type": "integer"},
                "unused": {"type": "boolean"},
                "statics": {"type": "boolean"},
                "dip_threshold": {"type": "integer"},
                "max_clone_distance": {"type": "integer"},
            },
        },
    },
    {
        "name": "antislop",
        "description": "Find confusing or pointless code fragments using the blerk index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "file_extensions": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "n": {"type": "integer"},
                "reset": {"type": "boolean", "description": "Clear existing confusing tags before sweeping."},
            },
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
        if args.get("directory"):
            cmd += ["--dir", args["directory"]]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        return _run(*cmd) or "No results found."

    if name == "browse":
        cmd = ["browse"]
        if args.get("directory"):
            cmd += ["--dir", args["directory"]]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        if args.get("symbols"):
            cmd.append("--symbols")
        return _run(*cmd) or "No indexed files found."

    if name == "detail":
        cmd = ["detail", args["name"]]
        if args.get("file_path"):
            cmd += ["--file", args["file_path"]]
        return _run(*cmd)

    if name == "deps":
        cmd = ["deps"]
        if args.get("directory"):
            cmd += ["--dir", args["directory"]]
        return _run(*cmd) or "No dependencies found."

    if name == "lint":
        cmd = ["lint"]
        if args.get("directory"):
            cmd += ["--dir", args["directory"]]
        for pattern in args.get("exclude", []):
            cmd += ["--exclude", pattern]
        cmd += [
            "--max-lines", str(args.get("max_lines", 40)),
            "--max-symbols", str(args.get("max_symbols", 20)),
            "--max-callees", str(args.get("max_callees", 8)),
            "--max-params", str(args.get("max_params", 4)),
            "--max-nesting", str(args.get("max_nesting", 3)),
            "--dip-threshold", str(args.get("dip_threshold", 3)),
            "--max-clone-distance", str(args.get("max_clone_distance", 3)),
        ]
        if args.get("unused"):
            cmd.append("--unused")
        if args.get("statics"):
            cmd.append("--statics")
        return _run(*cmd) or "No lint findings."

    if name == "antislop":
        cmd = ["confusing"]
        if args.get("directory"):
            cmd += ["--dir", args["directory"]]
        for ext in args.get("file_extensions", []):
            cmd += ["--ext", ext]
        for pattern in args.get("exclude", []):
            cmd += ["--exclude", pattern]
        if args.get("n") is not None:
            cmd += ["-n", str(args["n"])]
        if args.get("reset"):
            cmd.append("--reset")
        return _run(*cmd) or "No confusing fragments found."

    return f"Unknown tool: {name}"


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
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
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "blerk", "version": "0.1.0"},
            }})

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
