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
        cfg = config.load(config.default_path())
        conn = db.open_db(cfg.db.path)
        conn.execute(
            "INSERT INTO hint_extract_queue(transcript_path, cwd, priority) VALUES (?,?,10)",
            (transcript_path, cwd),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return 0
