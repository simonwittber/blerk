from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime


def fmt_duration(s: float) -> str:
    return f"{s * 1000:.0f}ms" if s < 1 else f"{s:.0f}s"


def setup_logging(silent: bool) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if silent:
        logging.getLogger().setLevel(logging.WARNING)


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


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
