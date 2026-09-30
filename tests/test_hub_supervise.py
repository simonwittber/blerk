from __future__ import annotations

import threading
import time

from blerk_cmd import hub


def _fast_backoff(monkeypatch, minimum=0.01, maximum=0.02, stable=0.05):
    monkeypatch.setattr(hub, "MIN_BACKOFF", minimum)
    monkeypatch.setattr(hub, "MAX_BACKOFF", maximum)
    monkeypatch.setattr(hub, "STABLE_RUN", stable)


def test_supervise_runs_the_function(monkeypatch):
    _fast_backoff(monkeypatch)
    shutdown = threading.Event()
    calls = []

    def fn():
        calls.append(1)
        shutdown.set()

    hub.supervise("t", fn, shutdown)
    assert calls == [1]


def test_supervise_restarts_after_a_crash(monkeypatch):
    _fast_backoff(monkeypatch)
    shutdown = threading.Event()
    calls = []

    def fn():
        calls.append(1)
        if len(calls) >= 3:
            shutdown.set()
        raise RuntimeError("boom")

    hub.supervise("t", fn, shutdown)
    assert len(calls) == 3, "a crashing daemon must be restarted, not abandoned"


def test_supervise_stops_when_shutdown_is_already_set(monkeypatch):
    _fast_backoff(monkeypatch)
    shutdown = threading.Event()
    shutdown.set()
    calls = []
    hub.supervise("t", lambda: calls.append(1), shutdown)
    assert calls == []


def test_supervise_does_not_restart_after_shutdown(monkeypatch):
    """A daemon that returns because shutdown was set must not be started again."""
    _fast_backoff(monkeypatch)
    shutdown = threading.Event()
    calls = []

    def fn():
        calls.append(1)
        shutdown.set()

    hub.supervise("t", fn, shutdown)
    assert calls == [1]


def test_supervise_backoff_grows_then_resets_after_a_stable_run(monkeypatch):
    monkeypatch.setattr(hub, "MIN_BACKOFF", 0.01)
    monkeypatch.setattr(hub, "MAX_BACKOFF", 10.0)
    monkeypatch.setattr(hub, "STABLE_RUN", 0.05)
    shutdown = threading.Event()
    waits: list[float] = []
    real_wait = shutdown.wait

    def spy(timeout=None):
        waits.append(timeout)
        return real_wait(0)          # never actually sleep

    monkeypatch.setattr(shutdown, "wait", spy)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 3:
            time.sleep(0.06)         # long enough to count as stable
        if len(calls) >= 5:
            shutdown.set()
        raise RuntimeError("boom")

    hub.supervise("t", fn, shutdown)
    # Run 1 and 2 fail fast so backoff doubles; run 3 lasts longer than STABLE_RUN so it resets.
    assert waits[0] == 0.01
    assert waits[1] > waits[0], "backoff must grow while the daemon keeps failing"
    assert waits[2] == 0.01, "backoff must reset after a stable run"
    assert waits[3] > waits[2], "and then grow again"


def test_supervise_caps_backoff_at_max(monkeypatch):
    monkeypatch.setattr(hub, "MIN_BACKOFF", 1.0)
    monkeypatch.setattr(hub, "MAX_BACKOFF", 2.0)
    monkeypatch.setattr(hub, "STABLE_RUN", 999.0)
    shutdown = threading.Event()
    waits: list[float] = []

    def spy(timeout=None):
        waits.append(timeout)
        return len(waits) >= 5       # stop after a few rounds

    monkeypatch.setattr(shutdown, "wait", spy)
    hub.supervise("t", lambda: (_ for _ in ()).throw(RuntimeError("boom")), shutdown)
    assert max(waits) <= 2.0
