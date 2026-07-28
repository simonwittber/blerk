from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class DB:
    path: str = ""


@dataclass
class Watch:
    folders: list[str] = field(default_factory=list)
    debounce_ms: int = 0
    ignore_file: str = ""


@dataclass
class Symbolizer:
    batch_size: int = 0
    poll_ms: int = 0
    max_retries: int = 0
    min_describe_lines: int = 0
    engine: str = ""


@dataclass
class GitEnricher:
    batch_size: int = 0
    poll_ms: int = 0
    max_retries: int = 0


@dataclass
class LLM:
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    batch_size: int = 0
    poll_ms: int = 0
    max_retries: int = 0
    max_context_chars: int = 0
    prompt_template: str = ""


@dataclass
class Embedder:
    endpoint: str = ""
    model: str = ""
    batch_size: int = 0
    poll_ms: int = 0
    vector_dim: int = 0
    max_retries: int = 0
    max_embed_chars: int = 0


@dataclass
class Config:
    secrets_file: str = ""
    db: DB = field(default_factory=DB)
    watch: Watch = field(default_factory=Watch)
    symbolizer: Symbolizer = field(default_factory=Symbolizer)
    git_enricher: GitEnricher = field(default_factory=GitEnricher)
    llm: LLM = field(default_factory=LLM)
    embedder: Embedder = field(default_factory=Embedder)


def defaults() -> Config:
    return Config(
        secrets_file="~/.blerk/secrets.toml",
        db=DB(path="~/.blerk/blerk.db"),
        watch=Watch(debounce_ms=100, ignore_file="~/.blerk/ignore"),
        symbolizer=Symbolizer(
            batch_size=10,
            poll_ms=1000,
            max_retries=3,
            min_describe_lines=5,
            engine="regexp",
        ),
        git_enricher=GitEnricher(
            batch_size=20,
            poll_ms=2000,
            max_retries=3,
        ),
        llm=LLM(
            endpoint="http://localhost:11434",
            model="llama3.2",
            batch_size=5,
            poll_ms=3000,
            max_retries=3,
            max_context_chars=16000,
            prompt_template='Describe the following {kind} named "{name}" from {path}. Be concise and technical.\n\n{context}',
        ),
        embedder=Embedder(
            endpoint="http://localhost:11434",
            model="nomic-embed-text",
            batch_size=10,
            poll_ms=2000,
            vector_dim=768,
            max_retries=3,
            max_embed_chars=8000,
        ),
    )


def default_path() -> str:
    return str(Path.home() / ".blerk" / "config.toml")


def expand_home(path: str) -> str:
    if path.startswith("~/") or path.startswith("~\\"):
        return str(Path.home() / path[2:])
    return path


def _merge(target, data: dict) -> None:
    for f in fields(target):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(target, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(target, f.name, value)


def load(path: str) -> Config:
    path = expand_home(path)
    cfg = defaults()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    _merge(cfg, data)

    cfg.db.path = expand_home(cfg.db.path)
    cfg.watch.folders = [expand_home(p) for p in cfg.watch.folders]
    cfg.watch.ignore_file = expand_home(cfg.watch.ignore_file)

    secrets_path = expand_home(cfg.secrets_file)
    try:
        with open(secrets_path, "rb") as f:
            secrets = tomllib.load(f)
        api_key = secrets.get("llm", {}).get("api_key", "")
        if api_key:
            cfg.llm.api_key = api_key
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        pass

    return cfg
