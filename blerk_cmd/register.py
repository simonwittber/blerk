from __future__ import annotations

import sys
from pathlib import Path

import tomllib
import tomli_w

from blerk import config


def _read(cfg_path: str) -> dict:
    with open(config.expand_home(cfg_path), "rb") as f:
        return tomllib.load(f)


def _write(cfg_path: str, data: dict) -> None:
    with open(config.expand_home(cfg_path), "wb") as f:
        tomli_w.dump(data, f)


def add_folder(cfg_path: str, path: str) -> int:
    norm = str(Path(path).resolve())
    data = _read(cfg_path)
    folders: list[str] = data.setdefault("watch", {}).setdefault("folders", [])
    if norm in folders:
        print(f"Already watching: {norm}")
        return 0
    folders.append(norm)
    _write(cfg_path, data)
    print(f"Added: {norm}")
    return 0


def remove_folder(cfg_path: str, path: str) -> int:
    norm = str(Path(path).resolve())
    data = _read(cfg_path)
    folders: list[str] = data.get("watch", {}).get("folders", [])
    if norm not in folders:
        print(f"Not in watch list: {norm}")
        return 1
    folders.remove(norm)
    data.setdefault("watch", {})["folders"] = folders
    _write(cfg_path, data)
    print(f"Removed: {norm}")
    return 0
