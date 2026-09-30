from __future__ import annotations

import struct

import httpx

# Every embedding goes to an OpenAI-compatible /v1/embeddings endpoint.
# Ollama serves that natively, as do vllm, LM Studio and the hosted providers, so blerk never loads model weights itself.
# That is the same protocol the describer and the reranker already speak.

_TIMEOUT = 120.0


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def unreachable(endpoint: str, detail: str) -> RuntimeError:
    """Build the error for an endpoint that did not answer, naming the two things that fix it."""
    return RuntimeError(
        f"cannot reach the embedding endpoint at {endpoint}: {detail}."
        " Start it (for Ollama, run 'ollama serve') or correct embedder.endpoint in config.toml."
    )


def embed_batch(endpoint: str, model: str, texts: list[str], api_key: str = "") -> list[list[float]]:
    """Embed several strings in one request.

    Results are ordered by the index field rather than by array position, because the OpenAI schema does not promise an order.
    """
    if not texts:
        return []
    if not endpoint:
        raise RuntimeError(
            "embedder.endpoint is not set in config.toml."
            " blerk embeds through an OpenAI-compatible server, so point it at one"
            " (for Ollama that is http://localhost:11434) or run 'blerk init'."
        )
    try:
        r = httpx.post(
            endpoint.rstrip("/") + "/v1/embeddings",
            json={"model": model, "input": texts},
            headers=_headers(api_key),
            timeout=_TIMEOUT,
        )
    except httpx.RequestError as e:
        raise unreachable(endpoint, str(e)) from e

    if r.status_code != 200:
        raise RuntimeError(f"embedding endpoint {r.status_code}: {r.text.strip()}")

    rows = r.json().get("data") or []
    if len(rows) != len(texts):
        raise RuntimeError(
            f"embedding endpoint returned {len(rows)} vectors for {len(texts)} inputs"
        )
    rows.sort(key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in rows]


def embed(endpoint: str, model: str, text: str, api_key: str = "") -> list[float]:
    """Embed a single string."""
    return embed_batch(endpoint, model, [text], api_key)[0]


def to_float32_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)
