from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from blerk import config, coordinator, daemon_util, db
from blerk.ignore_match import IgnoreSet, is_ignored, load_ignore_file

_conn_lock = db._write_lock


log = logging.getLogger("watch-folder")


class _Counter:
    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._n += 1

    def load(self) -> int:
        with self._lock:
            return self._n


_upsert_count = _Counter()


def _to_slash(p: str) -> str:
    return p.replace("\\", "/")


def hash_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_file(conn: sqlite3.Connection, path: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    mtime = int(st.st_mtime)
    size = int(st.st_size)

    stored = _to_slash(path)
    try:
        with _conn_lock:
            row = conn.execute(
                "SELECT mtime, size FROM files WHERE path=?", (stored,)
            ).fetchone()
    except sqlite3.Error as e:
        log.warning("upsert file select %s: %s", stored, e)
        return
    if row and int(row[0]) == mtime and int(row[1]) == size:
        return

    try:
        h = hash_file(path)
    except OSError:
        return

    try:
        with _conn_lock:
            conn.execute(
                "INSERT INTO files(path, mtime, size, hash) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, hash=excluded.hash "
                "WHERE excluded.hash != files.hash OR excluded.mtime != files.mtime OR excluded.size != files.size",
                (stored, mtime, size, h),
            )
    except sqlite3.Error as e:
        log.warning("upsert file %s: %s", stored, e)
        return
    _upsert_count.increment()


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    stored = _to_slash(path)
    try:
        with _conn_lock:
            conn.execute("DELETE FROM files WHERE path=?", (stored,))
    except sqlite3.Error as e:
        log.warning("delete file %s: %s", stored, e)


def beginning_of_day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day, 0, 0, 0, 0, tzinfo=t.tzinfo)


def _scan_dir(
    root: str,
    inherited: list[IgnoreSet],
    conn: sqlite3.Connection,
    all_sets: list[IgnoreSet],
) -> None:
    try:
        entries = list(os.scandir(root))
    except OSError:
        return

    sets = inherited
    for e in entries:
        if e.name == ".gitignore":
            try:
                patterns = load_ignore_file(e.path)
            except OSError:
                patterns = []
            if patterns:
                new_set = IgnoreSet(dir=root, patterns=patterns)
                sets = inherited + [new_set]
                all_sets.append(new_set)
            break

    for e in entries:
        p = e.path
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if is_ignored(p, True, sets):
                continue
            _scan_dir(p, sets, conn, all_sets)
        else:
            if not is_ignored(p, False, sets):
                upsert_file(conn, p)


class Debouncer:
    def __init__(self, delay_s: float, flush) -> None:
        self._delay = delay_s
        self._flush = flush
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._gen = 0

    def add(self, path: str, event: str) -> None:
        with self._lock:
            self._pending[path] = event
            if self._timer is not None:
                self._timer.cancel()
            self._gen += 1
            gen = self._gen
            self._timer = threading.Timer(self._delay, self._fire, args=(gen,))
            self._timer.daemon = True
            self._timer.start()

    def _fire(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            events = self._pending
            self._pending = {}
        self._flush(events)

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._gen += 1


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        conn: sqlite3.Connection,
        debouncer: Debouncer,
        get_sets,
    ) -> None:
        self._conn = conn
        self._deb = debouncer
        self._get_sets = get_sets

    def _ignored(self, path: str, is_dir: bool) -> bool:
        return is_ignored(path, is_dir, self._get_sets())

    def on_created(self, event) -> None:
        path = event.src_path
        if event.is_directory:
            if self._ignored(path, True):
                return
            sets = self._get_sets()
            try:
                entries = list(os.scandir(path))
            except OSError:
                return
            for e in entries:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    continue
                if not is_ignored(e.path, False, sets):
                    self._deb.add(e.path, "create")
            return
        if self._ignored(path, False):
            return
        self._deb.add(path, "create")

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        path = event.src_path
        if self._ignored(path, False):
            return
        self._deb.add(path, "modify")

    def on_deleted(self, event) -> None:
        path = event.src_path
        if event.is_directory:
            return
        self._deb.add(path, "remove")

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        src = event.src_path
        dest = event.dest_path
        self._deb.add(src, "remove")
        if not self._ignored(dest, False):
            self._deb.add(dest, "create")


def watch_folder(
    folder: str,
    conn: sqlite3.Connection,
    ignore_flag: str,
    debounce_s: float,
    scan_only: bool,
    shutdown: threading.Event,
    db_path: str = "",
    silent: bool = False,
):
    ignore_path = ignore_flag

    root_sets: list[IgnoreSet] = []
    if os.path.exists(ignore_path):
        try:
            patterns = load_ignore_file(ignore_path)
            if patterns:
                root_sets.append(IgnoreSet(dir=folder, patterns=patterns))
        except OSError as e:
            log.warning("load ignore %s: %s", ignore_path, e)

    client = coordinator.CoordinatorClient("symbol_queue", db_path) if db_path else None

    all_sets: list[IgnoreSet] = list(root_sets)
    _scan_dir(folder, root_sets, conn, all_sets)
    if client:
        client.notify("symbol_queue")

    if scan_only:
        if client:
            client.close()
        return None

    seen: set[str] = set()
    deduped: list[IgnoreSet] = []
    for s in all_sets:
        if s.dir not in seen:
            seen.add(s.dir)
            deduped.append(s)
    sets_lock = threading.Lock()
    live_sets = deduped

    def get_sets() -> list[IgnoreSet]:
        with sets_lock:
            return list(live_sets)

    def flush(events: dict[str, str]) -> None:
        upserted = 0
        for path, ev in events.items():
            if ev == "remove":
                delete_file(conn, path)
            else:
                upsert_file(conn, path)
                upserted += 1
        if upserted:
            if not silent:
                log.info("watch-folder: %d file(s)", upserted)
            if client:
                client.notify("symbol_queue")

    debouncer = Debouncer(debounce_s, flush)
    handler = _Handler(conn, debouncer, get_sets)
    observer = Observer()
    observer.schedule(handler, folder, recursive=True)
    observer.start()
    return observer


def start_heartbeat_thread(conn: sqlite3.Connection, shutdown: threading.Event) -> threading.Thread:
    def run() -> None:
        def write_stats(processed_today: int) -> None:
            with _conn_lock:
                row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            total = int(row[0]) if row else 0
            try:
                db.write_heartbeat(
                    conn, "watch-folder", "running", total, processed_today, 0, 0, 0.0, None, ""
                )
            except sqlite3.Error as e:
                log.warning("heartbeat: %s", e)

        write_stats(0)
        last_count = 0
        day_start = beginning_of_day(datetime.now())
        processed_today = 0

        while not shutdown.is_set():
            if shutdown.wait(timeout=30):
                return
            current = _upsert_count.load()
            delta = current - last_count
            last_count = current

            now = datetime.now()
            if (now - day_start).total_seconds() >= 24 * 3600:
                day_start = beginning_of_day(now)
                processed_today = 0
            processed_today += delta

            write_stats(processed_today)

    t = threading.Thread(target=run, name="watch-heartbeat", daemon=True)
    t.start()
    return t


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--ignore", default="", help="override ignore file path (default: watch.ignore_file from config)")
    parser.add_argument("--scan", action="store_true", help="exit after initial scan without watching for changes")
    parser.add_argument("--folder", default="", help="watch a single folder (overrides config folder list)")
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()

    cfg = config.load(args.config)
    daemon_util.setup_logging(args.silent or cfg.silent)
    conn = db.open_db(cfg.db.path)

    debounce_s = cfg.watch.debounce_ms / 1000.0
    shutdown = threading.Event()

    def _sig(_signum, _frame):
        shutdown.set()

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    silent = args.silent or cfg.silent
    if not args.scan:
        start_heartbeat_thread(conn, shutdown)

    observers = []
    ignore_path = args.ignore or cfg.watch.ignore_file
    folders = [args.folder] if args.folder else cfg.watch.folders
    for folder in folders:
        obs = watch_folder(folder, conn, ignore_path, debounce_s, args.scan, shutdown, cfg.db.path, silent)
        if obs is not None:
            observers.append(obs)

    if args.scan:
        return

    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=1.0)
    except KeyboardInterrupt:
        shutdown.set()

    for obs in observers:
        obs.stop()
    for obs in observers:
        obs.join()


if __name__ == "__main__":
    main()
