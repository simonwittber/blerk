from __future__ import annotations

import os
import struct
import threading
from typing import Optional

import httpx

_st_model: Optional[object] = None
_st_lock = threading.Lock()


def _get_sentence_transformer(model: str, device: str, cache_dir: str):
    global _st_model
    if _st_model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError("sentence-transformers not installed; install with: pip install sentence-transformers")

        device_to_use = device
        if device_to_use == "auto":
            try:
                import torch
                device_to_use = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device_to_use = "cpu"

        cache_path = os.path.expanduser(cache_dir)
        os.makedirs(cache_path, exist_ok=True)
        _st_model = SentenceTransformer(model, device=device_to_use, cache_folder=cache_path)
    return _st_model


def embed(backend: str, endpoint: str, model: str, text: str, device: str = "auto", cache_dir: str = "~/.cache/huggingface") -> list[float]:
    if backend == "ollama":
        r = httpx.post(
            endpoint + "/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=30.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"ollama {r.status_code}: {r.text}")
        return r.json()["embedding"]
    elif backend == "sentence-transformers":
        st = _get_sentence_transformer(model, device, cache_dir)
        with _st_lock:
            vecs = st.encode([text], convert_to_numpy=True)
        return vecs[0].tolist()
    else:
        raise RuntimeError(f"unknown embedding backend: {backend}")


def to_float32_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)
