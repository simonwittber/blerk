from __future__ import annotations

from pathlib import Path

import pytest

from blerk import config


def write_cfg(tmp_path: Path, content: str) -> str:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_load_full(tmp_path: Path) -> None:
    toml = """
[db]
path = "/abs/db.sqlite"

[watch]
folders = ["/a", "/b"]
debounce_ms = 250

[symbolizer]
batch_size = 3
poll_ms = 500

[git_enricher]
batch_size = 4
poll_ms = 600

[llm]
endpoint = "http://ollama:11434"
model = "codellama"
batch_size = 5
poll_ms = 700
max_retries = 6
max_context_chars = 8000
prompt_template = "T {name}"

[embedder]
endpoint = "http://ollama:11434"
model = "nomic"
batch_size = 7
poll_ms = 800
vector_dim = 512
max_retries = 9
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.db.path == "/abs/db.sqlite"
    assert cfg.watch.folders == ["/a", "/b"]
    assert cfg.watch.debounce_ms == 250
    assert cfg.symbolizer.batch_size == 3
    assert cfg.symbolizer.poll_ms == 500
    assert cfg.git_enricher.batch_size == 4
    assert cfg.git_enricher.poll_ms == 600
    assert cfg.llm[0].endpoint == "http://ollama:11434"
    assert cfg.llm[0].model == "codellama"
    assert cfg.llm[0].batch_size == 5
    assert cfg.llm[0].poll_ms == 700
    assert cfg.llm[0].max_retries == 6
    assert cfg.llm[0].max_context_chars == 8000
    assert cfg.llm[0].prompt_template == "T {name}"
    assert cfg.embedder.endpoint == "http://ollama:11434"
    assert cfg.embedder.model == "nomic"
    assert cfg.embedder.batch_size == 7
    assert cfg.embedder.poll_ms == 800
    assert cfg.embedder.vector_dim == 512
    assert cfg.embedder.max_retries == 9


def test_load_defaults_applied(tmp_path: Path) -> None:
    toml = """
[db]
path = "/abs/db.sqlite"
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.symbolizer.batch_size == 10
    assert cfg.symbolizer.poll_ms == 1000
    assert cfg.symbolizer.max_retries == 3
    assert cfg.symbolizer.min_describe_lines == 5
    assert cfg.git_enricher.batch_size == 20
    assert cfg.git_enricher.poll_ms == 2000
    assert cfg.git_enricher.max_retries == 3
    assert cfg.llm[0].max_context_chars == 16000
    assert cfg.llm[0].endpoint == "http://localhost:11434"
    assert cfg.llm[0].model == "llama3.2"
    assert cfg.llm[0].batch_size == 5
    assert cfg.llm[0].poll_ms == 3000
    assert cfg.llm[0].max_retries == 3
    assert cfg.embedder.vector_dim == 768
    assert cfg.embedder.max_embed_chars == 8000
    assert cfg.embedder.endpoint == "http://localhost:11434"
    assert cfg.embedder.model == "nomic-embed-text"
    assert cfg.embedder.batch_size == 10
    assert cfg.embedder.poll_ms == 2000
    assert cfg.embedder.max_retries == 3
    assert cfg.watch.debounce_ms == 100


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        config.load(str(tmp_path / "nonexistent.toml"))


def test_load_invalid_toml(tmp_path: Path) -> None:
    p = write_cfg(tmp_path, "this is = not = valid toml [[")
    import tomllib
    with pytest.raises(tomllib.TOMLDecodeError):
        config.load(p)


def test_load_expand_home_db_path(tmp_path: Path) -> None:
    toml = """
[db]
path = "~/data/foo.db"
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    want_prefix = str(Path.home() / "data")
    assert cfg.db.path.startswith(want_prefix)


def test_load_expand_home_watch_folders(tmp_path: Path) -> None:
    toml = """
[db]
path = "/abs/db.sqlite"

[watch]
folders = ["~/x", "~/y/z"]
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    home = Path.home()
    assert cfg.watch.folders[0] == str(home / "x").replace("\\", "/")
    assert cfg.watch.folders[1] == str(home / "y" / "z").replace("\\", "/")


def test_load_expand_home_absolute_unchanged(tmp_path: Path) -> None:
    toml = """
[db]
path = "/abs/db.sqlite"

[watch]
folders = ["/abs/x"]
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.db.path == "/abs/db.sqlite"
    assert cfg.watch.folders[0] == "/abs/x"


def test_default_path() -> None:
    want = str(Path.home() / ".blerk" / "config.toml")
    assert config.default_path() == want


def test_defaults_values() -> None:
    d = config.defaults()
    assert d.secrets_file == "~/.blerk/secrets.toml"
    assert d.db.path == "~/.blerk/blerk.db"
    assert d.llm[0].model == "llama3.2"
    assert d.embedder.vector_dim == 768


def test_secrets_merge(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text('[llm]\napi_key = "sk-test-123"\n', encoding="utf-8")
    toml = f"""
secrets_file = "{secrets_path.as_posix()}"

[db]
path = "/abs/db.sqlite"
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.llm[0].api_key == "sk-test-123"


def test_secrets_missing_ok(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    toml = f"""
secrets_file = "{missing.as_posix()}"

[db]
path = "/abs/db.sqlite"
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.llm[0].api_key == ""


def test_secrets_empty_api_key_does_not_override(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text('[llm]\napi_key = ""\n', encoding="utf-8")
    toml = f"""
secrets_file = "{secrets_path.as_posix()}"

[db]
path = "/abs/db.sqlite"

[llm]
api_key = "from-config"
"""
    cfg = config.load(write_cfg(tmp_path, toml))
    assert cfg.llm[0].api_key == "from-config"
