from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from blerk import config, coordinator, db
from blerk_cmd import (
    embedder,
    fingerprinter,
    git_enricher,
    knowledge_dedup,
    knowledge_extractor,
    knowledge_refiner,
    llm_describer,
    symbolizer,
    watch_folder,
)
from blerk_cmd.util import resolve_path

PID_FILE = Path.home() / ".blerk" / "blerk.pid"
# Graceful stop is requested through a file rather than a signal.
# On Windows os.kill(pid, SIGTERM) becomes TerminateProcess, which gives the hub no chance to run cleanup.
STOP_FILE = Path.home() / ".blerk" / "blerk.stop"


MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
STABLE_RUN = 30.0
CONFIG_POLL_S = 5.0

DAEMONS = [
    ("git-enricher",  git_enricher.run),
    ("embedder",      embedder.run),
    ("fingerprinter", fingerprinter.run),
]

DAEMON = "knowledge-extractor"

log = logging.getLogger("hub")


def supervise(name: str, fn: Callable[[], None], shutdown_event: threading.Event) -> None:
    """Run a daemon function in this thread, restarting it with backoff until shutdown.

    Replaces the previous subprocess supervisor.
    The daemons were always written as run(cfg, shutdown, ...) loops over a threading.Event,
    so the subprocess layer was wrapping something already shaped for a thread.
    Backoff behaviour is unchanged: 1s doubling to 60s, reset to 1s after STABLE_RUN seconds of uptime.
    """
    backoff = MIN_BACKOFF
    while not shutdown_event.is_set():
        start = time.monotonic()
        try:
            fn()
        except Exception:
            log.exception("[hub] %s crashed", name)
        else:
            if shutdown_event.is_set():
                log.info("[hub] stopped %s", name)
                return
            log.info("[hub] %s returned (restart in %.0fs)", name, backoff)

        if time.monotonic() - start >= STABLE_RUN:
            backoff = MIN_BACKOFF
        if shutdown_event.wait(timeout=backoff):
            return
        backoff = min(backoff * 2, MAX_BACKOFF)


def pid_is_alive(pid: int) -> bool:
    """Return True when a process with this pid exists."""
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_hub_pid() -> int | None:
    """Return the pid of a live hub, or None when there is none.

    A PID file left behind by a killed hub is removed, because otherwise start would refuse forever.
    Without this check, repeated `blerk start` calls stacked whole sets of daemons on top of each other.
    """
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid_is_alive(pid):
        return pid
    PID_FILE.unlink(missing_ok=True)
    return None


def _purge_folder(db_path: str, folder: str) -> None:
    prefix = resolve_path(folder) + "/"
    try:
        conn = db.open_db(db_path)
        with db._write_lock:
            conn.execute("DELETE FROM file_paths WHERE path LIKE ?", (prefix + "%",))
            conn.commit()
        conn.close()
        log.info("[hub] purged DB records for %s", folder)
    except Exception as e:
        log.warning("[hub] purge DB for %s failed: %s", folder, e)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    args = parser.parse_args()

    existing = running_hub_pid()
    if existing is not None:
        log.error("[hub] already running (pid %d). Run 'blerk stop' first.", existing)
        sys.exit(1)

    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        log.error("[hub] load config: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("[hub] load config: %s", e)
        sys.exit(1)

    shutdown = threading.Event()

    def _sig(_signum, _frame):
        log.info("[hub] received signal, shutting down")
        shutdown.set()

    signal.signal(signal.SIGINT, _sig)
    # SIGTERM is best-effort on Windows: Python maps it to a console handler that
    # only fires when TerminateProcess is called via an external tool.
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    cfg = config.load(args.config)

    # Initialize database before starting daemons to avoid concurrent WAL mode setup
    try:
        init_conn = db.open_db(cfg.db.path, init_schema=False)
        init_conn.close()
    except Exception as e:
        log.error("[hub] failed to initialize database: %s", e)
        sys.exit(1)

    coordinator.CoordinatorServer(cfg.db.path).start(shutdown)

    threads: list[threading.Thread] = []

    def spawn(name: str, fn: Callable[[], None], stop: threading.Event | None = None,
              track: bool = True) -> threading.Thread:
        t = threading.Thread(target=supervise, args=(name, fn, stop or shutdown), name=name, daemon=False)
        t.start()
        if track:
            threads.append(t)
        return t

    silent = cfg.silent
    for name, run_fn in DAEMONS:
        spawn(name, lambda f=run_fn: f(cfg, shutdown, silent))

    n_sym = max(1, cfg.symbolizer.workers)
    for i in range(n_sym):
        daemon_name = "symbolizer" if n_sym == 1 else f"symbolizer-{i}"
        spawn(daemon_name, lambda n=daemon_name: symbolizer.run(cfg, shutdown, silent, daemon_name=n))

    if cfg.knowledge.llm.enabled:
        spawn(DAEMON, lambda: knowledge_extractor.run(cfg, shutdown))
        spawn("knowledge-dedup", lambda: knowledge_dedup.run(cfg, shutdown))
        spawn("knowledge-refiner", lambda: knowledge_refiner.run(cfg, shutdown))

    llms = cfg.llm
    for i, llm in enumerate(llms):
        if not llm.enabled:
            continue
        daemon_name = "llm-describer" if len(llms) == 1 else f"llm-describer-{i}"
        spawn(daemon_name, lambda l=llm, n=daemon_name: llm_describer.run(cfg, l, shutdown, n, silent))

    # Per-folder watcher threads: {folder: (thread, folder_shutdown_event)}
    folder_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def _spawn_watcher(folder: str) -> tuple[threading.Thread, threading.Event]:
        folder_shutdown = threading.Event()
        # Watchers are tracked in folder_threads instead, because config reload stops them individually.
        t = spawn(
            f"watch-folder:{folder}",
            lambda f=folder, ev=folder_shutdown: watch_folder.run(cfg, ev, folders=[f], silent=silent),
            folder_shutdown,
            track=False,
        )
        log.info("[hub] started watcher for %s", folder)
        return t, folder_shutdown

    def _stop_watcher(folder: str) -> None:
        entry = folder_threads.pop(folder, None)
        if entry:
            t, ev = entry
            ev.set()
            t.join(timeout=10)
            log.info("[hub] stopped watcher for %s", folder)

    current_folders: set[str] = set(cfg.watch.folders)
    for folder in current_folders:
        folder_threads[folder] = _spawn_watcher(folder)

    cfg_path = args.config
    try:
        cfg_mtime = os.path.getmtime(cfg_path)
    except OSError:
        cfg_mtime = 0.0

    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=CONFIG_POLL_S)
            if shutdown.is_set():
                break

            if STOP_FILE.exists():
                log.info("[hub] stop requested, shutting down")
                shutdown.set()
                break

            try:
                new_mtime = os.path.getmtime(cfg_path)
            except OSError:
                continue
            if new_mtime == cfg_mtime:
                continue
            cfg_mtime = new_mtime

            try:
                new_cfg = config.load(cfg_path)
            except Exception as e:
                log.warning("[hub] config reload failed: %s", e)
                continue

            new_folders: set[str] = set(new_cfg.watch.folders)

            for folder in current_folders - new_folders:
                _stop_watcher(folder)
                _purge_folder(new_cfg.db.path, folder)

            for folder in new_folders - current_folders:
                folder_threads[folder] = _spawn_watcher(folder)

            current_folders = new_folders

    except KeyboardInterrupt:
        shutdown.set()
    finally:
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)

    for folder in list(folder_threads):
        _stop_watcher(folder)

    for t in threads:
        t.join(timeout=10)

    log.info("[hub] done")


if __name__ == "__main__":
    main()
