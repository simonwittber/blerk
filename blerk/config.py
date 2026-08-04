from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace as dc_replace
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
    workers: int = 1


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


_DEFAULT_RERANKER_PROMPT = """\
Rank these code symbols by relevance to: "{query_text}"
Reply with only comma-separated indices, most relevant first.

{numbered}"""


@dataclass
class Reranker:
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    enabled: bool = False
    prompt: str = _DEFAULT_RERANKER_PROMPT



@dataclass
class AnalyzerRule:
    name: str = ""
    severity: str = "warning"
    description: str = ""


@dataclass
class Analyzer:
    name: str = ""
    description: str = ""
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    min_lines: int = 5
    kinds: list[str] = field(default_factory=lambda: ["function", "method"])
    extensions: list[str] = field(default_factory=list)
    confidence: float = 0.7
    max_context_callers: int = 3
    max_context_callees: int = 5
    rules: list[AnalyzerRule] = field(default_factory=list)


@dataclass
class Coordinator:
    port: int = 0


@dataclass
class Suppress:
    path: str = ""
    rules: list[str] = field(default_factory=list)


@dataclass
class Lint:
    suppress: list[Suppress] = field(default_factory=list)


@dataclass
class Config:
    secrets_file: str = ""
    analyzers_file: str = ""
    db: DB = field(default_factory=DB)
    watch: Watch = field(default_factory=Watch)
    symbolizer: Symbolizer = field(default_factory=Symbolizer)
    git_enricher: GitEnricher = field(default_factory=GitEnricher)
    llm: list[LLM] = field(default_factory=list)
    embedder: Embedder = field(default_factory=Embedder)
    reranker: Reranker = field(default_factory=Reranker)
    coordinator: Coordinator = field(default_factory=Coordinator)
    lint: Lint = field(default_factory=Lint)
    silent: bool = False


def defaults() -> Config:
    return Config(
        secrets_file="~/.blerk/secrets.toml",
        analyzers_file="~/.blerk/analyzers.toml",
        db=DB(path="~/.blerk/blerk.db"),
        watch=Watch(debounce_ms=100, ignore_file="~/.blerk/ignore"),
        symbolizer=Symbolizer(
            batch_size=10,
            poll_ms=1000,
            max_retries=3,
            min_describe_lines=5,
        ),
        git_enricher=GitEnricher(
            batch_size=20,
            poll_ms=2000,
            max_retries=3,
        ),
        llm=[LLM(
            endpoint="http://localhost:11434",
            model="llama3.2",
            batch_size=5,
            poll_ms=3000,
            max_retries=3,
            max_context_chars=16000,
            prompt_template='Describe the following {kind} named "{name}" from {path}. Be concise and technical.\n\n{context}',
        )],
        reranker=Reranker(
            endpoint="http://localhost:11434",
            model="",
            enabled=False,
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

    if "lint" in data:
        lint_data = data["lint"]
        suppress_raw = lint_data.pop("suppress", [])
        cfg.lint.suppress = [
            Suppress(path=s.get("path", ""), rules=s.get("rules", []))
            for s in suppress_raw
        ]

    if "llm" in data:
        llm_data = data.pop("llm")
        if isinstance(llm_data, dict):
            llm_data = [llm_data]
        default_llm = defaults().llm[0]
        cfg.llm = []
        for d in llm_data:
            entry = dc_replace(default_llm)
            _merge(entry, d)
            cfg.llm.append(entry)

    _merge(cfg, data)

    cfg.db.path = expand_home(cfg.db.path)
    cfg.analyzers_file = expand_home(cfg.analyzers_file)
    def _realpath_slash(p: str) -> str:
        real = os.path.realpath(p)
        return real.replace("\\", "/") if os.path.exists(real) else p.replace("\\", "/")

    cfg.watch.folders = [_realpath_slash(expand_home(p)) for p in cfg.watch.folders]
    cfg.watch.ignore_file = expand_home(cfg.watch.ignore_file)

    secrets_path = expand_home(cfg.secrets_file)
    try:
        with open(secrets_path, "rb") as f:
            secrets = tomllib.load(f)
        api_key = secrets.get("llm", {}).get("api_key", "")
        if api_key:
            for llm in cfg.llm:
                if not llm.api_key:
                    llm.api_key = api_key
            if not cfg.reranker.api_key:
                cfg.reranker.api_key = api_key
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        pass

    return cfg


def load_analyzers_file(path: str) -> list[Analyzer]:
    path = expand_home(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    result: list[Analyzer] = []
    for a in data.get("analyzers", []):
        rules = [
            AnalyzerRule(
                name=r.get("name", ""),
                severity=r.get("severity", "warning"),
                description=r.get("description", ""),
            )
            for r in a.get("rules", [])
        ]
        result.append(Analyzer(
            name=a.get("name", ""),
            description=a.get("description", ""),
            endpoint=a.get("endpoint", ""),
            model=a.get("model", ""),
            api_key=a.get("api_key", ""),
            min_lines=a.get("min_lines", 5),
            kinds=a.get("kinds", ["function", "method"]),
            extensions=a.get("extensions", []),
            confidence=a.get("confidence", 0.7),
            max_context_callers=a.get("max_context_callers", 3),
            max_context_callees=a.get("max_context_callees", 5),
            rules=rules,
        ))
    return result
