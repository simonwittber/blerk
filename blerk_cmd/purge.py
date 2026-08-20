from __future__ import annotations

import argparse
import os
import subprocess
import sys

from blerk import config, db, ignore_match


def purge(conn, ignore_file: str, dry_run: bool = False) -> int:
    patterns = ignore_match.load_ignore_file(ignore_file)
    ignore_set = ignore_match.IgnoreSet(dir="/", patterns=patterns)

    rows = conn.execute("SELECT id, path FROM files").fetchall()
    to_delete: list[int] = []
    for file_id, path in rows:
        norm = path.replace("\\", "/")
        if ignore_match.is_ignored(norm, is_dir=False, sets=[ignore_set]):
            to_delete.append(file_id)
            if dry_run:
                print(path)

    if not to_delete:
        return 0

    if not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", to_delete)
        conn.commit()

    return len(to_delete)


def purge_missing(conn, dry_run: bool = False) -> int:
    rows = conn.execute("SELECT id, path FROM files").fetchall()
    to_delete: list[int] = []
    for file_id, path in rows:
        if not os.path.exists(path):
            to_delete.append(file_id)
            if dry_run:
                print(f"[missing] {path}")

    if not to_delete:
        return 0

    if not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", to_delete)
        conn.commit()

    return len(to_delete)


def _worktree_paths(folder: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    paths: list[str] = []
    first = True
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if first:
                first = False
                continue
            paths.append(os.path.normpath(path))
    return paths


def purge_worktrees(conn, folders: list[str], dry_run: bool = False) -> int:
    worktree_roots: list[str] = []
    for folder in folders:
        worktree_roots.extend(_worktree_paths(folder))

    if not worktree_roots:
        return 0

    rows = conn.execute("SELECT id, path FROM files").fetchall()
    to_delete: list[int] = []
    for file_id, path in rows:
        norm = os.path.normpath(path)
        for root in worktree_roots:
            if norm.startswith(root + os.sep) or norm == root:
                to_delete.append(file_id)
                if dry_run:
                    print(f"[worktree] {path}")
                break

    if not to_delete:
        return 0

    if not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", to_delete)
        conn.commit()

    return len(to_delete)


def _collect_ignore_sets(folder: str) -> list[ignore_match.IgnoreSet]:
    sets: list[ignore_match.IgnoreSet] = []

    def walk(root: str, inherited: list[ignore_match.IgnoreSet]) -> None:
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        current = inherited
        for e in entries:
            if e.name == ".gitignore":
                try:
                    patterns = ignore_match.load_ignore_file(e.path)
                except OSError:
                    patterns = []
                if patterns:
                    s = ignore_match.IgnoreSet(dir=root, patterns=patterns)
                    current = inherited + [s]
                    sets.append(s)
                break
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if ignore_match.is_ignored(e.path, True, current):
                        continue
                    if os.path.isfile(os.path.join(e.path, ".git")):
                        continue
                    walk(e.path, current)
            except OSError:
                continue

    walk(folder, [])
    return sets


def purge_gitignored(conn, folders: list[str], dry_run: bool = False) -> int:
    all_sets: list[ignore_match.IgnoreSet] = []
    for folder in folders:
        all_sets.extend(_collect_ignore_sets(folder))

    if not all_sets:
        return 0

    rows = conn.execute("SELECT id, path FROM files").fetchall()
    to_delete: list[int] = []
    for file_id, path in rows:
        if ignore_match.is_ignored(path, is_dir=False, sets=all_sets):
            to_delete.append(file_id)
            if dry_run:
                print(f"[gitignored] {path}")

    if not to_delete:
        return 0

    if not dry_run:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", to_delete)
        conn.commit()

    return len(to_delete)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove indexed files that should not be in the index.")
    parser.add_argument("--config", default=config.default_path())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-missing", action="store_true", help="Skip removing files missing from disk")
    parser.add_argument("--no-worktrees", action="store_true", help="Skip removing files from git worktrees")
    parser.add_argument("--no-gitignored", action="store_true", help="Skip removing gitignored files")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.open_db(cfg.db.path)
    total = 0

    if not args.no_missing:
        n = purge_missing(conn, dry_run=args.dry_run)
        if n:
            verb = "would remove" if args.dry_run else "removed"
            print(f"{verb} {n} missing file(s)")
        total += n

    folders = cfg.watch.folders
    if folders:
        if not args.no_worktrees:
            n = purge_worktrees(conn, folders, dry_run=args.dry_run)
            if n:
                verb = "would remove" if args.dry_run else "removed"
                print(f"{verb} {n} worktree file(s)")
            total += n

        if not args.no_gitignored:
            n = purge_gitignored(conn, folders, dry_run=args.dry_run)
            if n:
                verb = "would remove" if args.dry_run else "removed"
                print(f"{verb} {n} gitignored file(s)")
            total += n

    ignore_file = cfg.watch.ignore_file
    if ignore_file and os.path.exists(os.path.expanduser(ignore_file)):
        n = purge(conn, os.path.expanduser(ignore_file), dry_run=args.dry_run)
        if n:
            verb = "would remove" if args.dry_run else "removed"
            print(f"{verb} {n} explicitly ignored file(s)")
        total += n

    conn.close()

    if not total:
        print("Nothing to remove.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
