from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    transcript_path = data.get("transcript_path", "")
    if not transcript_path:
        return 0

    src = Path(transcript_path)
    if not src.exists():
        return 0

    dest_dir = Path.home() / ".blerk" / "transcripts"
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = int(time.time() * 1000)
    dest = dest_dir / f"{ts}.jsonl"

    try:
        shutil.copy2(src, dest)
    except OSError:
        return 0

    return 0
