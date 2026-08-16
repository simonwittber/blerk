from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from blerk import config, coordinator, db
from blerk_cmd.util import normalize_dir

PID_FILE = Path.home() / ".blerk" / "blerk.pid"


MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
STABLE_RUN = 30.0
CONFIG_POLL_S = 5.0

DAEMONS = [
    ("git-enricher",  "blerk_cmd.git_enricher"),
    ("embedder",      "blerk_cmd.embedder"),
    ("fingerprinter", "blerk_cmd.fingerprinter"),
]

DAEMON = "knowledge-extractor"

log = logging.getLogger("hub")


_POPEN_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32" else {}
)


def build_argv(module: str, cfg_path: str) -> list[str]:
    return [sys.executable, "-m", module, "--config", cfg_path]


def managed(name: str, argv: list[str], shutdown_event: threading.Event) -> None:
    backoff = MIN_BACKOFF
    while not shutdown_event.is_set():
        try:
            proc = subprocess.Popen(argv, stdout=None, stderr=None, **_POPEN_FLAGS)
        except OSError as e:
            log.warning("[hub] failed to start %s: %s (retry in %.0fs)", name, e, backoff)
            if shutdown_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        log.info("[hub] started %s (pid %d)", name, proc.pid)
        start = time.monotonic()

        while True:
            if shutdown_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                log.info("[hub] stopped %s", name)
                return
            rc = proc.poll()
            if rc is not None:
                break
            time.sleep(0.1)

        elapsed = time.monotonic() - start
        if elapsed >= STABLE_RUN:
            backoff = MIN_BACKOFF
        if rc != 0:
            log.warning("[hub] %s exited with code %d (retry in %.0fs)", name, rc, backoff)
        else:
            log.info("[hub] %s exited cleanly (retry in %.0fs)", name, backoff)

        if shutdown_event.wait(timeout=backoff):
            return
        backoff = min(backoff * 2, MAX_BACKOFF)


def _purge_folder(db_path: str, folder: str) -> None:
    prefix = normalize_dir(folder).rstrip("/") + "/"
    try:
        conn = db.open_db(db_path)
        with db._write_lock:
            conn.execute("DELETE FROM files WHERE path LIKE ?", (prefix + "%",))
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

    coord = coordinator.CoordinatorServer(cfg.db.path, cfg.coordinator.port)
    coord.start(shutdown)

    threads: list[threading.Thread] = []
    for name, module in DAEMONS:
        argv = build_argv(module, args.config)
        t = threading.Thread(target=managed, args=(name, argv, shutdown), name=name, daemon=False)
        t.start()
        threads.append(t)

    n_sym = max(1, cfg.symbolizer.workers)
    for i in range(n_sym):
        daemon_name = "symbolizer" if n_sym == 1 else f"symbolizer-{i}"
        argv = build_argv("blerk_cmd.symbolizer", args.config)
        t = threading.Thread(target=managed, args=(daemon_name, argv, shutdown), name=daemon_name, daemon=False)
        t.start()
        threads.append(t)

    if cfg.knowledge.llm.enabled:
        argv = build_argv("blerk_cmd.knowledge_extractor", args.config)
        t = threading.Thread(target=managed, args=(DAEMON, argv, shutdown), name=DAEMON, daemon=False)
        t.start()
        threads.append(t)

        dedup_argv = build_argv("blerk_cmd.knowledge_dedup", args.config)
        dedup_name = "knowledge-dedup"
        t = threading.Thread(target=managed, args=(dedup_name, dedup_argv, shutdown), name=dedup_name, daemon=False)
        t.start()
        threads.append(t)

        refiner_argv = build_argv("blerk_cmd.knowledge_refiner", args.config) + ["--daemon"]
        refiner_name = "knowledge-refiner"
        t = threading.Thread(target=managed, args=(refiner_name, refiner_argv, shutdown), name=refiner_name, daemon=False)
        t.start()
        threads.append(t)

    llms = cfg.llm
    for i, llm in enumerate(llms):
        if not llm.enabled:
            continue
        daemon_name = "llm-describer" if len(llms) == 1 else f"llm-describer-{i}"
        argv = build_argv("blerk_cmd.llm_describer", args.config) + [
            "--endpoint", llm.endpoint,
            "--model", llm.model,
            "--daemon-name", daemon_name,
        ]
        t = threading.Thread(target=managed, args=(daemon_name, argv, shutdown), name=daemon_name, daemon=False)
        t.start()
        threads.append(t)

    # Per-folder watcher threads: {folder: (thread, folder_shutdown_event)}
    folder_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def _spawn_watcher(folder: str) -> tuple[threading.Thread, threading.Event]:
        folder_shutdown = threading.Event()
        argv = build_argv("blerk_cmd.watch_folder", args.config) + ["--folder", folder]
        t = threading.Thread(
            target=managed,
            args=(f"watch-folder:{folder}", argv, folder_shutdown),
            name=f"watch-folder:{folder}",
            daemon=False,
        )
        t.start()
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

    PID_FILE.write_text(str(os.getpid()))
    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=CONFIG_POLL_S)
            if shutdown.is_set():
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

    for folder in list(folder_threads):
        _stop_watcher(folder)

    for t in threads:
        t.join(timeout=10)

    log.info("[hub] done")


if __name__ == "__main__":
    main()
