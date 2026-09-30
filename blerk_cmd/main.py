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
    "summary":      "blerk_cmd.summary",
    "knowledge-enqueue":  "blerk_cmd.knowledge_enqueue",
    "extract-knowledge":  "blerk_cmd.extract_knowledge",
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


def _stop(grace_s: float = 20.0) -> int:
    """Ask the hub to shut down, then make sure it did.

    A signal is not enough: on Windows os.kill(pid, SIGTERM) becomes TerminateProcess, so the hub dies
    without running cleanup. The hub polls for a stop file instead, which lets it exit gracefully on
    every platform. Killing is the fallback for a hub that is wedged and not polling.
    """
    import os
    import signal as _signal
    import time

    from blerk_cmd.hub import PID_FILE, STOP_FILE, pid_is_alive

    if not PID_FILE.exists():
        print("blerk: no running instance found (no PID file)")
        return 1
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        print("blerk stop: unreadable PID file, removing it")
        PID_FILE.unlink(missing_ok=True)
        return 1

    if not pid_is_alive(pid):
        print(f"blerk: hub (pid {pid}) is not running, clearing stale PID file")
        PID_FILE.unlink(missing_ok=True)
        return 0

    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text("stop")
    print(f"Asked blerk hub (pid {pid}) to stop...")

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            print("Stopped.")
            STOP_FILE.unlink(missing_ok=True)
            return 0
        time.sleep(0.25)

    print(f"blerk: hub did not stop within {grace_s:.0f}s, terminating it")
    try:
        os.kill(pid, _signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        print(f"blerk stop: {e}")
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)
    return 0


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
