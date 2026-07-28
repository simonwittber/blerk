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

from blerk import config

PID_FILE = Path.home() / ".blerk" / "blerk.pid"


MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
STABLE_RUN = 30.0

DAEMONS = [
    ("watch-folder", "blerk_cmd.watch_folder"),
    ("symbolizer", "blerk_cmd.symbolizer"),
    ("git-enricher", "blerk_cmd.git_enricher"),
    ("embedder", "blerk_cmd.embedder"),
]

log = logging.getLogger("hub")


def build_argv(module: str, cfg_path: str) -> list[str]:
    return [sys.executable, "-m", module, "--config", cfg_path]


def managed(name: str, argv: list[str], shutdown_event: threading.Event) -> None:
    backoff = MIN_BACKOFF
    while not shutdown_event.is_set():
        try:
            proc = subprocess.Popen(argv, stdout=None, stderr=None)
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

    threads: list[threading.Thread] = []
    for name, module in DAEMONS:
        argv = build_argv(module, args.config)
        t = threading.Thread(target=managed, args=(name, argv, shutdown), name=name, daemon=False)
        t.start()
        threads.append(t)

    llms = cfg.llm
    for i, llm in enumerate(llms):
        daemon_name = "llm-describer" if len(llms) == 1 else f"llm-describer-{i}"
        argv = build_argv("blerk_cmd.llm_describer", args.config) + [
            "--endpoint", llm.endpoint,
            "--model", llm.model,
            "--daemon-name", daemon_name,
        ]
        t = threading.Thread(target=managed, args=(daemon_name, argv, shutdown), name=daemon_name, daemon=False)
        t.start()
        threads.append(t)

    PID_FILE.write_text(str(os.getpid()))
    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=1.0)
    except KeyboardInterrupt:
        shutdown.set()
    finally:
        PID_FILE.unlink(missing_ok=True)

    for t in threads:
        t.join(timeout=10)

    log.info("[hub] done")


if __name__ == "__main__":
    main()
