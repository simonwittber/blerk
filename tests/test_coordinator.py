from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from blerk.coordinator import (
    CoordinatorClient,
    CoordinatorServer,
    _port_file,
    _workers_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wakeup(port: int) -> None:
    """Send a no-op UDP packet to unblock a recvfrom call."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"", ("127.0.0.1", port))
    s.close()


@pytest.fixture
def server_ctx(tmp_path):
    """Start a CoordinatorServer and tear it down after the test."""
    db_path = str(tmp_path / "blerk.db")
    shutdown = threading.Event()
    server = CoordinatorServer(db_path, port=0)
    server.start(shutdown)
    yield server, shutdown, db_path
    shutdown.set()
    _wakeup(server._bound_port)
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# CoordinatorServer tests
# ---------------------------------------------------------------------------

class TestCoordinatorServer:
    def test_start_writes_port_file(self, server_ctx):
        server, _, db_path = server_ctx
        pf = _port_file(db_path)
        assert pf.exists()
        assert int(pf.read_text().strip()) == server._bound_port

    def test_port_file_removed_on_shutdown(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        shutdown = threading.Event()
        server = CoordinatorServer(db_path, port=0)
        server.start(shutdown)
        pf = _port_file(db_path)
        assert pf.exists()
        shutdown.set()
        _wakeup(server._bound_port)
        time.sleep(0.1)
        assert not pf.exists()

    def test_route_sends_check_to_registered_worker(self, server_ctx, tmp_path):
        server, _, db_path = server_ctx
        worker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        worker_sock.bind(("127.0.0.1", 0))
        worker_sock.settimeout(1.0)
        worker_port = worker_sock.getsockname()[1]

        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)
        wf = wd / f"myqueue-{os.getpid()}.worker"
        wf.write_text(f"pid={os.getpid()}\nport={worker_port}\nqueue=myqueue\n")

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            send_sock.sendto(b"NOTIFY myqueue", ("127.0.0.1", server._bound_port))
            data, _ = worker_sock.recvfrom(64)
            assert data.strip() == b"CHECK"
        finally:
            worker_sock.close()
            send_sock.close()

    def test_route_ignores_unknown_queue(self, server_ctx, tmp_path):
        server, _, db_path = server_ctx
        worker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        worker_sock.bind(("127.0.0.1", 0))
        worker_sock.settimeout(0.3)

        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)
        wd.joinpath(f"queue_a-{os.getpid()}.worker").write_text(
            f"pid={os.getpid()}\nport={worker_sock.getsockname()[1]}\nqueue=queue_a\n"
        )

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            send_sock.sendto(b"NOTIFY queue_b", ("127.0.0.1", server._bound_port))
            with pytest.raises(socket.timeout):
                worker_sock.recvfrom(64)
        finally:
            worker_sock.close()
            send_sock.close()

    def test_route_round_robins_across_workers(self, server_ctx, tmp_path):
        server, _, db_path = server_ctx
        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)

        socks = []
        for i in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.settimeout(0.5)
            port = s.getsockname()[1]
            wd.joinpath(f"rrq-{1000 + i}.worker").write_text(
                f"pid={os.getpid()}\nport={port}\nqueue=rrq\n"
            )
            socks.append(s)

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            send_sock.sendto(b"NOTIFY rrq", ("127.0.0.1", server._bound_port))
            time.sleep(0.05)
            send_sock.sendto(b"NOTIFY rrq", ("127.0.0.1", server._bound_port))
            time.sleep(0.1)

            checks_received = 0
            for s in socks:
                try:
                    s.recvfrom(64)
                    checks_received += 1
                except socket.timeout:
                    pass
            assert checks_received == 2
        finally:
            for s in socks:
                s.close()
            send_sock.close()

    def test_non_notify_message_ignored(self, server_ctx, tmp_path):
        server, _, db_path = server_ctx
        worker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        worker_sock.bind(("127.0.0.1", 0))
        worker_sock.settimeout(0.3)

        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)
        wd.joinpath(f"q-{os.getpid()}.worker").write_text(
            f"pid={os.getpid()}\nport={worker_sock.getsockname()[1]}\nqueue=q\n"
        )

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            send_sock.sendto(b"BOGUS q", ("127.0.0.1", server._bound_port))
            with pytest.raises(socket.timeout):
                worker_sock.recvfrom(64)
        finally:
            worker_sock.close()
            send_sock.close()


# ---------------------------------------------------------------------------
# CoordinatorClient tests
# ---------------------------------------------------------------------------

class TestCoordinatorClient:
    def test_register_writes_worker_file(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("myq", db_path)
        wf = _workers_dir(db_path) / f"myq-{os.getpid()}.worker"
        try:
            assert wf.exists()
            content = wf.read_text()
            assert f"pid={os.getpid()}" in content
            assert "queue=myq" in content
        finally:
            client.close()

    def test_register_cleans_up_stale_worker_files(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        wd = _workers_dir(db_path)
        wd.mkdir(parents=True, exist_ok=True)
        stale = wd / "myq-99999999.worker"
        stale.write_text("pid=99999999\nport=12345\nqueue=myq\n")
        client = CoordinatorClient("myq", db_path)
        try:
            assert not stale.exists()
        finally:
            client.close()

    def test_close_removes_worker_file(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("q", db_path)
        wf = _workers_dir(db_path) / f"q-{os.getpid()}.worker"
        assert wf.exists()
        client.close()
        assert not wf.exists()

    def test_wait_returns_false_on_timeout(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("q", db_path)
        shutdown = threading.Event()
        try:
            start = time.monotonic()
            result = client.wait(shutdown, timeout_s=0.1)
            assert result is False
            assert time.monotonic() - start < 1.0
        finally:
            client.close()

    def test_wait_returns_true_when_shutdown_set(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("q", db_path)
        shutdown = threading.Event()
        shutdown.set()
        try:
            result = client.wait(shutdown, timeout_s=5.0)
            assert result is True
        finally:
            client.close()

    def test_wait_returns_false_on_check_message(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("q", db_path)
        shutdown = threading.Event()

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def _send_check():
            time.sleep(0.05)
            sender.sendto(b"CHECK", ("127.0.0.1", client._port))

        t = threading.Thread(target=_send_check, daemon=True)
        t.start()
        try:
            result = client.wait(shutdown, timeout_s=2.0)
            assert result is False
        finally:
            sender.close()
            client.close()

    def test_notify_sends_udp_to_hub(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.bind(("127.0.0.1", 0))
        recv_sock.settimeout(1.0)
        recv_port = recv_sock.getsockname()[1]
        _port_file(db_path).write_text(str(recv_port))

        client = CoordinatorClient("q", db_path)
        try:
            client.notify("target_queue")
            data, _ = recv_sock.recvfrom(256)
            assert data == b"NOTIFY target_queue"
        finally:
            recv_sock.close()
            client.close()

    def test_notify_silently_skips_when_no_port_file(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        client = CoordinatorClient("q", db_path)
        try:
            client.notify("q")  # no port file; should not raise
        finally:
            client.close()

    def test_hub_port_cached_after_first_read(self, tmp_path):
        db_path = str(tmp_path / "blerk.db")
        _port_file(db_path).write_text("54321")
        client = CoordinatorClient("q", db_path)
        try:
            port = client._hub_port_cached()
            assert port == 54321
            _port_file(db_path).unlink()
            assert client._hub_port_cached() == 54321
        finally:
            client.close()
