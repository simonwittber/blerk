from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import datetime
from typing import Callable

from blerk import config


def fmt_duration(s: float) -> str:
    return f"{s * 1000:.0f}ms" if s < 1 else f"{s:.0f}s"


def setup_logging(silent: bool) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if silent:
        logging.getLogger().setLevel(logging.WARNING)


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


def daemon_main(run_fn: Callable) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()
    try:
        cfg = config.load(args.config)
    except (FileNotFoundError, OSError) as e:
        logging.getLogger(__name__).error("load config: %s", e)
        sys.exit(1)
    setup_logging(args.silent or cfg.silent)
    shutdown = make_shutdown()
    run_fn(cfg, shutdown, silent=args.silent or cfg.silent)


def make_shutdown() -> threading.Event:
    shutdown = threading.Event()

    def _sig(_signum, _frame):
        shutdown.set()

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass
    return shutdown
