from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("coordinator")


def _workers_dir(db_path: str) -> Path:
    return Path(db_path).parent / "workers"


def _port_file(db_path: str) -> Path:
    return Path(db_path).parent / "coordinator.port"


def _is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class CoordinatorServer:
    """UDP server run by hub to route NOTIFY messages to idle workers."""

    def __init__(self, db_path: str, port: int = 0) -> None:
        self._db_path = db_path
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", port))
        self._bound_port = self._sock.getsockname()[1]
        self._sock.settimeout(0.5)
        self._rr: dict[str, int] = {}

    def start(self, shutdown: threading.Event) -> None:
        _port_file(self._db_path).write_text(str(self._bound_port))
        t = threading.Thread(
            target=self._run, args=(shutdown,), name="coordinator", daemon=True
        )
        t.start()
        log.info("[coordinator] listening on port %d", self._bound_port)

    def _run(self, shutdown: threading.Event) -> None:
        try:
            while not shutdown.is_set():
                try:
                    data, _ = self._sock.recvfrom(256)
                except (socket.timeout, OSError):
                    continue
                msg = data.decode("utf-8", errors="ignore").strip()
                if msg.startswith("NOTIFY "):
                    queue = msg[7:].strip()
                    self._route(queue)
        finally:
            self._sock.close()
            _port_file(self._db_path).unlink(missing_ok=True)

    def _route(self, queue: str) -> None:
        workers_dir = _workers_dir(self._db_path)
        if not workers_dir.exists():
            return
        candidates: list[int] = []
        for f in workers_dir.glob("*.worker"):
            try:
                data: dict[str, str] = {}
                for line in f.read_text().splitlines():
                    k, _, v = line.partition("=")
                    data[k.strip()] = v.strip()
                if data.get("queue") != queue:
                    continue
                pid = int(data["pid"])
                port = int(data["port"])
                if _is_alive(pid):
                    candidates.append(port)
            except (OSError, ValueError, KeyError):
                continue
        if not candidates:
            return
        idx = self._rr.get(queue, 0) % len(candidates)
        self._rr[queue] = idx + 1
        try:
            self._sock.sendto(b"CHECK", ("127.0.0.1", candidates[idx]))
        except OSError as e:
            log.debug("send CHECK to port %d: %s", candidates[idx], e)


class CoordinatorClient:
    """Per-daemon UDP client. Registers with the coordinator and waits for CHECK signals."""

    def __init__(self, queue: str, db_path: str) -> None:
        self._queue = queue
        self._db_path = db_path
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._port = self._sock.getsockname()[1]
        self._hub_port: int | None = None
        self._worker_file = _workers_dir(db_path) / f"{queue}-{os.getpid()}.worker"
        self._register()

    def _hub_port_cached(self) -> int | None:
        if self._hub_port is not None:
            return self._hub_port
        try:
            text = _port_file(self._db_path).read_text().strip()
            self._hub_port = int(text)
        except (OSError, ValueError):
            pass
        return self._hub_port

    def _register(self) -> None:
        workers_dir = _workers_dir(self._db_path)
        workers_dir.mkdir(parents=True, exist_ok=True)
        for f in workers_dir.glob(f"{self._queue}-*.worker"):
            try:
                lines = {k: v for k, _, v in (l.partition("=") for l in f.read_text().splitlines()) if k}
                if not _is_alive(int(lines["pid"])):
                    f.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                pass
        self._worker_file.write_text(
            f"pid={os.getpid()}\nport={self._port}\nqueue={self._queue}\n"
        )

    def notify(self, queue: str) -> None:
        hub_port = self._hub_port_cached()
        if hub_port is None:
            return
        try:
            self._sock.sendto(f"NOTIFY {queue}".encode(), ("127.0.0.1", hub_port))
        except OSError:
            pass

    def wait(self, shutdown: threading.Event, timeout_s: float) -> bool:
        """Block until CHECK arrives, timeout fires, or shutdown is set.

        Returns True if shutdown was requested (caller should break), False otherwise.
        """
        deadline = time.monotonic() + timeout_s
        while not shutdown.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._sock.settimeout(min(remaining, 0.05))
            try:
                data, _ = self._sock.recvfrom(64)
                if data.strip() == b"CHECK":
                    return False
            except (socket.timeout, OSError):
                pass
        return True

    def close(self) -> None:
        try:
            self._worker_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
