from __future__ import annotations

import json
import sys

from blerk import config, db


def main(argv: list[str] | None = None) -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd", "")
    if not transcript_path:
        return 0

    try:
        content = open(transcript_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return 0

    if not content.strip():
        return 0

    try:
        cfg = config.load(config.default_path())
        conn = db.open_db(cfg.db.path)
        conn.execute(
            "INSERT INTO transcripts(path, cwd, content) VALUES (?,?,?)",
            (transcript_path, cwd, content),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return 0
