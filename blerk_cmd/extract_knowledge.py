from __future__ import annotations

import argparse
import json
import re
import sqlite3
import struct
import sys
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from blerk import config as _config_mod

_client = httpx.Client(timeout=120.0)
_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


_FILE_INPUT_KEYS = ("file_path", "path", "notebook_path", "filePath")


def filter_transcript(content: str, max_chars: int, inject_files: bool = False) -> str:
    lines: list[str] = []
    file_refs: set[str] = set()
    for raw in content.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        role = obj.get("type", "")
        if role not in ("user", "assistant"):
            continue
        msg = obj.get("message", {})
        body = msg.get("content", "")
        if isinstance(body, list):
            if inject_files:
                for block in body:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        inp = block.get("input", {})
                        for key in _FILE_INPUT_KEYS:
                            val = inp.get(key)
                            if isinstance(val, str) and val:
                                file_refs.add(val)
            parts = [b.get("text", "") for b in body if isinstance(b, dict) and b.get("type") == "text"]
            body = " ".join(parts)
        if not isinstance(body, str):
            continue
        body = body.strip()
        if not body:
            continue
        lines.append(f"{role}: {body}")

    # Keep the most recent content by trimming from the start
    total = 0
    kept: list[str] = []
    for line in reversed(lines):
        if total + len(line) > max_chars:
            break
        kept.append(line)
        total += len(line)
    kept.reverse()

    result = "\n".join(kept)
    if inject_files and file_refs:
        header = "Files referenced in this session:\n" + "\n".join(sorted(file_refs))
        result = header + "\n\n" + result
    return result


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    if not text:
        return []

    raw_voices = re.split(r'(?=^(?:user|assistant):)', text, flags=re.MULTILINE)
    voices = [v for v in raw_voices if v.strip()]

    conversation: list[list[str]] = []
    for voice in voices:
        speaker = voice.split(":", 1)[0]
        if conversation and conversation[-1][0].split(":", 1)[0] == speaker:
            conversation[-1].append(voice)
        else:
            conversation.append([voice])

    chunks = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(conversation), step):
        start = max(0, i - overlap)
        chunk = conversation[start:start + chunk_size]
        chunks.append("".join("".join(turn) for turn in chunk).strip())

    return chunks


def call_llm(endpoint: str, model: str, api_key: str, prompt: str) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = _client.post(endpoint + "/v1/chat/completions", json=body, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"llm {r.status_code}: {r.text[:200]}")
    choices = r.json().get("choices", [])
    if not choices:
        raise RuntimeError("empty llm response")
    return choices[0]["message"]["content"]


def parse_knowledge(text: str) -> list[dict]:
    m = _JSON_RE.search(text)
    if not m:
        return []
    try:
        items = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept", "")).strip()
        pattern = str(item.get("pattern", "**")).strip()
        body = str(item.get("body", "")).strip()
        if concept and body:
            result.append({"concept": concept, "pattern": pattern or "**", "body": body})
    return result


_MERGE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def _unpack(data: bytes) -> list[float]:
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def get_embedding(cfg: "_config_mod.Config", text: str) -> list[float]:
    from blerk import embedding as _emb
    emb = cfg.embedder
    return _emb.embed(emb.backend, emb.endpoint, emb.model, text, emb.device, emb.cache_dir)


def embed_knowledge(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    knowledge_id: int,
    body: str,
) -> tuple[list[float], bytes]:
    vec = get_embedding(cfg, body)
    blob = struct.pack(f"<{len(vec)}f", *vec)
    model = cfg.embedder.model
    conn.execute(
        "INSERT INTO knowledge_embeddings(knowledge_id, vector, model) VALUES (?,?,?)",
        (knowledge_id, blob, model),
    )
    conn.commit()
    return vec, blob


def _classify(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    id_new: int, body_new: str, concept_new: str, pattern_new: str,
    id_old: int, body_old: str, concept_old: str, pattern_old: str,
) -> None:
    llm = cfg.knowledge.llm
    prompt = (
        cfg.knowledge.extractor.classify_prompt
        .replace("{body_a}", body_new)
        .replace("{body_b}", body_old)
    )
    try:
        response = call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
        m = _MERGE_RE.search(response)
        if not m:
            return
        result = json.loads(m.group())
    except Exception:
        return

    action = result.get("action")

    if action == "merge":
        merged_concept = result.get("concept", concept_old)
        merged_pattern = result.get("pattern", pattern_old if pattern_old != "**" else pattern_new)
        merged_body = result.get("body", body_old)
        conn.execute(
            "UPDATE knowledge SET concept=?, pattern=?, body=?, importance=importance+1 WHERE id=?",
            (merged_concept, merged_pattern, merged_body, id_old),
        )
        conn.execute("DELETE FROM knowledge WHERE id=?", (id_new,))
        conn.commit()

    elif action == "contradict":
        row_new = conn.execute("SELECT created_at FROM knowledge WHERE id=?", (id_new,)).fetchone()
        row_old = conn.execute("SELECT created_at FROM knowledge WHERE id=?", (id_old,)).fetchone()
        if row_new and row_old:
            older_id = id_new if (row_new[0] or 0) < (row_old[0] or 0) else id_old
        else:
            older_id = id_old
        conn.execute(
            "UPDATE knowledge SET suppressed_at=unixepoch() WHERE id=?", (older_id,)
        )
        conn.execute(
            "INSERT INTO knowledge_contradictions(id_a, id_b) VALUES (?,?)",
            (id_new, id_old),
        )
        conn.commit()


def dedup_knowledge(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    knowledge_id: int,
    vec: list[float],
    body: str,
    concept: str,
    pattern: str,
) -> None:
    model = cfg.embedder.model
    threshold = cfg.knowledge.extractor.dedup_threshold

    rows = conn.execute(
        "SELECT ke.knowledge_id, ke.vector, k.concept, k.pattern, k.body"
        " FROM knowledge_embeddings ke"
        " JOIN knowledge k ON k.id = ke.knowledge_id"
        " WHERE ke.model = ? AND ke.knowledge_id != ?",
        (model, knowledge_id),
    ).fetchall()

    for other_id, other_blob, other_concept, other_pattern, other_body in rows:
        other_vec = _unpack(other_blob)
        sim = _cosine_sim(vec, other_vec)
        if sim >= threshold:
            _classify(conn, cfg, knowledge_id, body, concept, pattern,
                      other_id, other_body, other_concept, other_pattern)
            return


def embed_and_dedup(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    knowledge_id: int,
    body: str,
    concept: str,
    pattern: str,
) -> None:
    vec, _ = embed_knowledge(conn, cfg, knowledge_id, body)
    dedup_knowledge(conn, cfg, knowledge_id, vec, body, concept, pattern)


def _merge(
    conn: sqlite3.Connection,
    cfg: "_config_mod.Config",
    id_new: int, body_new: str, concept_new: str, pattern_new: str,
    id_old: int, body_old: str, concept_old: str, pattern_old: str,
) -> None:
    llm = cfg.knowledge.llm
    merge_prompt = (
        cfg.knowledge.extractor.merge_prompt
        .replace("{body_a}", body_new)
        .replace("{body_b}", body_old)
    )
    try:
        response = call_llm(llm.endpoint, llm.model, llm.api_key, merge_prompt)
        m = _MERGE_RE.search(response)
        if not m:
            raise ValueError("no JSON object in merge response")
        merged = json.loads(m.group())
    except Exception:
        merged = {
            "concept": concept_old,
            "pattern": pattern_old if pattern_old != "**" else pattern_new,
            "body": body_old,
        }

    conn.execute(
        "UPDATE knowledge SET concept=?, pattern=?, body=?, importance=importance+1 WHERE id=?",
        (merged.get("concept", concept_old),
         merged.get("pattern", pattern_old),
         merged.get("body", body_old),
         id_old),
    )
    conn.execute("DELETE FROM knowledge WHERE id=?", (id_new,))
    conn.commit()


_STAGES = ("filter", "extract", "embed", "dedup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract knowledge from a transcript file.")
    parser.add_argument("file", help="transcript JSONL file")
    parser.add_argument("--stage", choices=_STAGES, default=None,
                        help="stop after this stage and print output")
    parser.add_argument("--inject-files", action="store_true",
                        help="prepend referenced file paths to the filtered transcript")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    from blerk import config as _config
    cfg_path = args.config or _config.default_path()
    cfg = _config.load(cfg_path)
    llm = cfg.knowledge.llm
    extractor_cfg = cfg.knowledge.extractor

    try:
        content = open(args.file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    filtered = filter_transcript(content, llm.max_context_chars, inject_files=args.inject_files)
    if args.stage == "filter":
        print(filtered)
        return 0

    if not filtered:
        print("empty after filter", file=sys.stderr)
        return 0

    print(f"filtered transcript: {len(filtered)} chars", file=sys.stderr)
    prompt = llm.prompt_template.replace("{transcript}", filtered)
    try:
        response = call_llm(llm.endpoint, llm.model, llm.api_key, prompt)
    except Exception as e:
        print(f"llm error: {e}", file=sys.stderr)
        return 1

    print(f"response: {response}", file=sys.stderr)
    items = parse_knowledge(response)
    if args.stage == "extract":
        for item in items:
            print(json.dumps(item))
        return 0

    from blerk import db as _db
    conn = _db.open_db(cfg.db.path)

    inserted: list[tuple[int, dict]] = []
    for item in items:
        cur = conn.execute(
            "INSERT INTO knowledge(concept, pattern, body, source) VALUES (?,?,?,?)",
            (item["concept"], item["pattern"], item["body"], "auto"),
        )
        conn.execute(
            "INSERT INTO knowledge_embed_queue(knowledge_id) VALUES (?)",
            (cur.lastrowid,),
        )
        conn.commit()
        inserted.append((cur.lastrowid, item))

    print(f"{len(inserted)} items queued for embedding", file=sys.stderr)

    if args.stage == "embed":
        for kid, item in inserted:
            vec, blob = embed_knowledge(conn, cfg, kid, item["body"])
            conn.execute(
                "INSERT INTO knowledge_dedup_queue(knowledge_id) VALUES (?)", (kid,)
            )
            conn.execute(
                "UPDATE knowledge_embed_queue SET status='done' WHERE knowledge_id=?", (kid,)
            )
            conn.commit()
            print(f"  embedded {item['concept']} ({len(vec)}d)", file=sys.stderr)
        conn.close()
        return 0

    for kid, item in inserted:
        print(f"  embed+dedup {item['concept']}", file=sys.stderr)
        vec, _ = embed_knowledge(conn, cfg, kid, item["body"])
        conn.execute("UPDATE knowledge_embed_queue SET status='done' WHERE knowledge_id=?", (kid,))
        conn.commit()
        dedup_knowledge(conn, cfg, kid, vec, item["body"], item["concept"], item["pattern"])
        conn.execute("UPDATE knowledge_dedup_queue SET status='done' WHERE knowledge_id=?", (kid,))
        conn.commit()

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
