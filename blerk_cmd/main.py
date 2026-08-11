from __future__ import annotations

import importlib
import sys

from blerk import config

_DISPATCH = {
    "init":      "blerk_cmd.init",
    "start":     "blerk_cmd.hub",
    "status":    "blerk_cmd.status",
    "query":     "blerk_cmd.query",
    "search":    "blerk_cmd.query",
    "browse":    "blerk_cmd.browse",
    "detail":    "blerk_cmd.detail",
    "show":      "blerk_cmd.show",
    "deps":      "blerk_cmd.deps",
    "lint":      "blerk_cmd.lint",
    "rescan":    "blerk_cmd.rescan",
    "reindex":   "blerk_cmd.reindex",
    "similar":   "blerk_cmd.similar",
    "purge":     "blerk_cmd.purge",
    "tags":      "blerk_cmd.tags",
    "analyze":   "blerk_cmd.analyze",
    "findings":  "blerk_cmd.findings",
    "summary":   "blerk_cmd.summary",
    "service":   "blerk_cmd.service",
}

_HELP = {
    "init":   "Initialise blerk configuration",
    "start":  "Start all daemons",
    "stop":   "Stop running daemons",
    "status": "Show daemon status",
    "query":  "Search indexed symbols",
    "search": "Search indexed symbols (alias for query)",
    "browse": "Browse indexed files and symbols",
    "detail": "Show full detail for a symbol by name",
    "show":   "Show source code for a file or symbol",
    "deps":   "Show file-level dependency graph",
    "lint":   "Lint code using the blerk index",
    "rescan": "Re-queue files for symbolization",
    "reindex": "Re-queue code blocks for embedding",
    "similar": "Find semantically similar code blocks",
    "purge":  "Remove indexed files that match ignore patterns",
    "tags":      "List all tag keys and values in the index",
    "analyze":   "Run LLM-based analyzers against indexed symbols",
    "findings":  "Show stored analyzer findings",
    "summary":   "Print a project index snapshot",
    "service":   "Manage blerk as a system service",
    "add":    "Add a folder to the watch list",
    "remove": "Remove a folder from the watch list",
}


def _usage() -> None:
    print("usage: blerk <command> [options]")
    print()
    print("Commands:")
    for cmd, help_text in _HELP.items():
        print(f"  {cmd:<10} {help_text}")


def _cfg_path_from_args() -> str:
    rest = sys.argv[2:]
    for i, arg in enumerate(rest):
        if arg == "--config" and i + 1 < len(rest):
            return rest[i + 1]
    return config.default_path()


def _dispatch(module_name: str, cmd: str) -> int:
    sys.argv = [f"blerk-{cmd}"] + sys.argv[2:]
    mod = importlib.import_module(module_name)
    return mod.main() or 0


def _stop() -> int:
    import os
    import signal as _signal
    from pathlib import Path

    pid_path = Path.home() / ".blerk" / "blerk.pid"
    if not pid_path.exists():
        print("blerk: no running instance found (no PID file)")
        return 1
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, _signal.SIGTERM)
        print(f"Sent SIGTERM to blerk hub (pid {pid})")
        return 0
    except (ValueError, ProcessLookupError, PermissionError) as e:
        print(f"blerk stop: {e}")
        pid_path.unlink(missing_ok=True)
        return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _usage()
        return 0

    cmd = sys.argv[1]

    if cmd in _DISPATCH:
        return _dispatch(_DISPATCH[cmd], cmd)

    if cmd == "stop":
        return _stop()

    cfg_path = _cfg_path_from_args()

    if cmd == "add":
        if len(sys.argv) < 3:
            print("usage: blerk add <path>")
            return 1
        from blerk_cmd.register import add_folder
        return add_folder(cfg_path, sys.argv[2])

    if cmd == "remove":
        if len(sys.argv) < 3:
            print("usage: blerk remove <path>")
            return 1
        from blerk_cmd.register import remove_folder
        return remove_folder(cfg_path, sys.argv[2])

    print(f"blerk: unknown command '{cmd}'")
    print("Run 'blerk --help' for usage.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
