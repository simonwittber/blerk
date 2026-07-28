from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time

from blerk import config


MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
STABLE_RUN = 30.0

DAEMONS = [
    ("watch-folder", "blerk_cmd.watch_folder"),
    ("symbolizer", "blerk_cmd.symbolizer"),
    ("git-enricher", "blerk_cmd.git_enricher"),
    ("llm-describer", "blerk_cmd.llm_describer"),
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
        config.load(args.config)
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

    threads: list[threading.Thread] = []
    for name, module in DAEMONS:
        argv = build_argv(module, args.config)
        t = threading.Thread(
            target=managed,
            args=(name, argv, shutdown),
            name=name,
            daemon=False,
        )
        t.start()
        threads.append(t)

    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=1.0)
    except KeyboardInterrupt:
        shutdown.set()

    for t in threads:
        t.join(timeout=10)

    log.info("[hub] done")


if __name__ == "__main__":
    main()
