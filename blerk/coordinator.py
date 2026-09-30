from __future__ import annotations

import logging
import threading

log = logging.getLogger("coordinator")

# Wake-ups used to travel over UDP, because each daemon was its own process.
# That needed a socket per daemon, a port file, a *.worker file per daemon on disk, and liveness checks
# to tell a registered worker from a crashed one.
# The daemons now run as threads in the hub, so the whole mechanism is a shared registry of Events.
# The class and method names are unchanged so no daemon loop had to be touched.

_lock = threading.Lock()
_waiters: dict[str, list[threading.Event]] = {}
_next: dict[str, int] = {}


def _register(queue: str) -> threading.Event:
    event = threading.Event()
    with _lock:
        _waiters.setdefault(queue, []).append(event)
    return event


def _unregister(queue: str, event: threading.Event) -> None:
    with _lock:
        waiters = _waiters.get(queue)
        if waiters and event in waiters:
            waiters.remove(event)


def _wake_one(queue: str) -> None:
    """Wake a single waiter on this queue, round robin, matching the old routing."""
    with _lock:
        waiters = list(_waiters.get(queue, ()))
        if not waiters:
            return
        idx = _next.get(queue, 0) % len(waiters)
        _next[queue] = idx + 1
    waiters[idx].set()


def reset() -> None:
    """Drop all registrations. For tests."""
    with _lock:
        _waiters.clear()
        _next.clear()


class CoordinatorServer:
    """Kept so the hub's startup reads the same. Routing is now in-process, so there is nothing to serve."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def start(self, shutdown: threading.Event) -> None:
        log.info("[coordinator] in-process wake-ups active")


class CoordinatorClient:
    """Per-daemon handle for waiting on work and notifying other queues."""

    def __init__(self, queue: str, db_path: str) -> None:
        self._queue = queue
        self._event = _register(queue)

    def notify(self, queue: str) -> None:
        _wake_one(queue)

    def wait(self, shutdown: threading.Event, timeout_s: float) -> bool:
        """Block until notified, the timeout fires, or shutdown is set.

        Returns True if shutdown was requested (caller should break), False otherwise.
        """
        if shutdown.is_set():
            return True
        if self._event.wait(timeout=timeout_s):
            self._event.clear()
        return shutdown.is_set()

    def close(self) -> None:
        _unregister(self._queue, self._event)
