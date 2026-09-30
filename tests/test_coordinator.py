from __future__ import annotations

import threading
import time

import pytest

from blerk import coordinator


@pytest.fixture(autouse=True)
def clean_registry():
    coordinator.reset()
    yield
    coordinator.reset()


def test_wait_returns_false_on_timeout():
    c = coordinator.CoordinatorClient("q", "")
    shutdown = threading.Event()
    t0 = time.monotonic()
    assert c.wait(shutdown, 0.05) is False
    assert time.monotonic() - t0 >= 0.04
    c.close()


def test_wait_returns_true_when_shutdown_already_set():
    c = coordinator.CoordinatorClient("q", "")
    shutdown = threading.Event()
    shutdown.set()
    assert c.wait(shutdown, 5.0) is True, "must not block when shutdown is already requested"
    c.close()


def test_wait_returns_true_when_shutdown_arrives_during_the_wait():
    c = coordinator.CoordinatorClient("q", "")
    shutdown = threading.Event()
    threading.Timer(0.05, shutdown.set).start()
    assert c.wait(shutdown, 5.0) is True
    c.close()


def test_notify_wakes_a_waiter_early():
    c = coordinator.CoordinatorClient("q", "")
    shutdown = threading.Event()
    threading.Timer(0.05, lambda: coordinator._wake_one("q")).start()
    t0 = time.monotonic()
    assert c.wait(shutdown, 5.0) is False
    assert time.monotonic() - t0 < 2.0, "notify must cut the wait short"
    c.close()


def test_notify_only_affects_the_named_queue():
    a = coordinator.CoordinatorClient("qa", "")
    b = coordinator.CoordinatorClient("qb", "")
    a.notify("qb")
    assert b.wait(threading.Event(), 0.01) is False
    t0 = time.monotonic()
    a.wait(threading.Event(), 0.05)
    assert time.monotonic() - t0 >= 0.04, "the other queue must not have been woken"
    a.close()
    b.close()


def test_notify_round_robins_across_waiters():
    clients = [coordinator.CoordinatorClient("q", "") for _ in range(3)]
    for _ in range(3):
        coordinator._wake_one("q")
    # One notify per waiter means every waiter returns immediately.
    for c in clients:
        assert c.wait(threading.Event(), 0.01) is False
    for c in clients:
        c.close()


def test_wait_clears_the_event_so_the_next_wait_blocks():
    c = coordinator.CoordinatorClient("q", "")
    coordinator._wake_one("q")
    assert c.wait(threading.Event(), 5.0) is False
    t0 = time.monotonic()
    c.wait(threading.Event(), 0.05)
    assert time.monotonic() - t0 >= 0.04, "a consumed notify must not satisfy the next wait"
    c.close()


def test_close_unregisters():
    c = coordinator.CoordinatorClient("q", "")
    c.close()
    assert coordinator._waiters.get("q") == []


def test_notify_with_no_waiters_is_harmless():
    coordinator._wake_one("nobody")
