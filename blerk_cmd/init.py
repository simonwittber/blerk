from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

_BLERK_DIR = Path.home() / ".blerk"

_CONFIG_TEMPLATE = """\
secrets_file = "~/.blerk/secrets.toml"

[db]
path = "~/.blerk/blerk.db"

[watch]
folders = {folders}
debounce_ms = 100
ignore_file = "~/.blerk/ignore"

[symbolizer]
engine = "treesitter"
batch_size = 10
poll_ms = 1000
max_retries = 3
min_describe_lines = 5

[git_enricher]
batch_size = 20
poll_ms = 2000
max_retries = 3

{llm_section}

[embedder]
backend = {embed_backend!r}
endpoint = {embed_endpoint!r}
model = {embed_model!r}
batch_size = 10
poll_ms = 2000
max_retries = 3
max_embed_chars = 2000
device = {embed_device!r}
cache_dir = {embed_cache_dir!r}
"""

_SECRETS_TEMPLATE = """\
[llm]
api_key = {api_key!r}
"""

_DEFAULT_IGNORE = """\
# Version control
.git/
.svn/
.hg/

# Claude Code configuration
.claude/

# Build output
bin/
obj/
out/
build/
dist/
target/
*.exe
*.dll
*.so
*.dylib
*.pyd
*.pyc
*.pyo
*.class
*.o
*.a
*.lib

# Caches
.cache/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
.tox/
node_modules/
.npm/
.yarn/
.nuget/
.gradle/
.m2/

# IDE and editor
.vs/
.vscode/
.idea/
*.suo
*.user
*.sln.docstates
.DS_Store
Thumbs.db

# Logs and temp files
*.log
*.tmp
*.temp
*.bak
*.swp
*.lock

# Unity
Library/
PackageCache/
Temp/
Logs/
UserSettings/
*.meta
~UnityDirMonSyncFile~*

# Unity binary assets (textures, audio, video, models, fonts)
*.png
*.jpg
*.jpeg
*.tga
*.tiff
*.tif
*.psd
*.gif
*.bmp
*.exr
*.hdr
*.wav
*.mp3
*.ogg
*.aiff
*.aif
*.mp4
*.mov
*.avi
*.webm
*.fbx
*.obj
*.dae
*.blend
*.ttf
*.otf
*.cubemap
*.unitypackage

# Python virtualenvs
.venv/
venv/
env/

# Docker
.docker/

# Coverage
.coverage
htmlcov/
coverage.xml
"""


def _check_ollama(endpoint: str) -> list[str]:
    try:
        import httpx
        resp = httpx.get(f"{endpoint}/api/tags", timeout=3.0)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def _prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{message}{suffix}: ").strip()
    return val or default


def _prompt_folders() -> list[str]:
    print("Watch folders (one path per line, blank line to finish):")
    folders: list[str] = []
    while True:
        val = input("  > ").strip()
        if not val:
            if folders:
                break
            print("  At least one folder is required.")
            continue
        p = Path(val).expanduser().resolve()
        if not p.exists():
            print(f"  Warning: {p} does not exist.")
        folders.append(str(p).replace("\\", "/"))
    return folders


def _toml_string_list(items: list[str]) -> str:
    escaped = [v.replace("\\", "\\\\") for v in items]
    inner = ", ".join(f'"{v}"' for v in escaped)
    return f"[{inner}]"


def _get_default_embed_model(backend: str) -> str:
    """Get appropriate default embedding model for backend."""
    if backend == "sentence-transformers":
        return "all-MiniLM-L6-v2"
    return "nomic-embed-text"


def _load_existing_config(config_path: Path) -> dict:
    """Load existing config and extract current values as defaults."""
    defaults = {
        "folders": [],
        "llm_enabled": True,
        "llm_endpoint": "http://localhost:11434",
        "llm_model": "llama3.2",
        "embed_backend": "ollama",
        "embed_endpoint": "http://localhost:11434",
        "embed_model": "nomic-embed-text",
        "embed_device": "auto",
        "embed_cache_dir": "~/.cache/huggingface",
    }
    if not config_path.exists():
        return defaults
    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
        if "watch" in cfg and "folders" in cfg["watch"]:
            defaults["folders"] = cfg["watch"]["folders"]
        if "llm" in cfg:
            if isinstance(cfg["llm"], list) and cfg["llm"]:
                llm = cfg["llm"][0]
                defaults["llm_enabled"] = llm.get("enabled", True)
                defaults["llm_endpoint"] = llm.get("endpoint", defaults["llm_endpoint"])
                defaults["llm_model"] = llm.get("model", defaults["llm_model"])
            elif isinstance(cfg["llm"], dict):
                defaults["llm_enabled"] = cfg["llm"].get("enabled", True)
                defaults["llm_endpoint"] = cfg["llm"].get("endpoint", defaults["llm_endpoint"])
                defaults["llm_model"] = cfg["llm"].get("model", defaults["llm_model"])
        if "embedder" in cfg:
            embed = cfg["embedder"]
            defaults["embed_backend"] = embed.get("backend", defaults["embed_backend"])
            defaults["embed_endpoint"] = embed.get("endpoint", defaults["embed_endpoint"])
            defaults["embed_model"] = embed.get("model", defaults["embed_model"])
            defaults["embed_device"] = embed.get("device", defaults["embed_device"])
            defaults["embed_cache_dir"] = embed.get("cache_dir", defaults["embed_cache_dir"])
    except Exception as e:
        print(f"Warning: could not parse existing config: {e}")
    return defaults


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    dry_run = args.dry_run

    _BLERK_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _BLERK_DIR / "config.toml"
    secrets_path = _BLERK_DIR / "secrets.toml"
    ignore_path = _BLERK_DIR / "ignore"

    print(f"blerk init: {_BLERK_DIR}")
    print()

    # Load existing config for defaults
    existing = _load_existing_config(config_path)

    if config_path.exists() and not dry_run:
        ans = input("Config already exists. Reconfigure? [y/N]: ").strip().lower()
        if ans != "y":
            print("Skipping config.")
            return 0
        print()

    if dry_run:
        llm_enabled = existing["llm_enabled"]
        llm_endpoint = existing["llm_endpoint"]
        llm_model = existing["llm_model"]
        embed_backend = existing["embed_backend"]
        embed_endpoint = existing["embed_endpoint"]
        embed_model = existing["embed_model"]
        embed_device = existing["embed_device"]
        embed_cache_dir = existing["embed_cache_dir"]
        api_key = ""
        folders = existing["folders"]
        available_models = []
    else:
        # Enable descriptions?
        enable_llm = input("Enable code descriptions? [Y/n]: ").strip().lower()
        llm_enabled = enable_llm != "n"
        print()

        # Embedding backend (decide early)
        print("Embedding backend options:")
        print("  ollama - Use Ollama instance")
        print("  sentence-transformers - Use HuggingFace models locally (no server needed)")
        embed_backend = _prompt("Embedding backend", existing["embed_backend"])
        if embed_backend not in ("ollama", "sentence-transformers"):
            embed_backend = "ollama"
            print(f"  Invalid backend; using 'ollama'")

        # Update default embed_model based on backend choice
        existing["embed_model"] = _get_default_embed_model(embed_backend)
        print()

        # Only ask for/check Ollama endpoint if using Ollama backend
        available_models = []
        if embed_backend == "ollama":
            ollama_endpoint = _prompt("Ollama endpoint", existing["embed_endpoint"])
            print(f"\nChecking Ollama at {ollama_endpoint}...")
            available_models = _check_ollama(ollama_endpoint)
            if available_models:
                print(f"  OK — {len(available_models)} model(s) available:")
                for m in available_models:
                    print(f"    {m}")
            else:
                print("  Could not reach Ollama. Check that it is running.")
                print("  Continuing with defaults — edit config.toml later if needed.")
            print()
        else:
            # sentence-transformers doesn't need Ollama
            ollama_endpoint = existing["embed_endpoint"]

        # LLM configuration only if enabled
        if llm_enabled:
            llm_endpoint = _prompt("LLM endpoint (Ollama)", existing["llm_endpoint"])
            llm_model = _prompt("LLM model", existing["llm_model"])
            api_key = ""
            needs_key = input("Does the LLM endpoint require an API key? [y/N]: ").strip().lower()
            if needs_key == "y":
                api_key = input("API key: ").strip()
        else:
            llm_endpoint = existing["llm_endpoint"]
            llm_model = existing["llm_model"]
            api_key = ""
        print()

        # Embedding backend-specific settings
        if embed_backend == "ollama":
            embed_endpoint = ollama_endpoint
            embed_model = _prompt("Embedding model (Ollama)", existing["embed_model"])
            embed_device = "auto"
            embed_cache_dir = "~/.cache/huggingface"

            def _model_available(name: str, models: list[str]) -> bool:
                name_base = name.split(":")[0]
                return any(m == name or m.split(":")[0] == name_base for m in models)

            if available_models and not _model_available(embed_model, available_models):
                print(f"  Warning: '{embed_model}' is not in the available model list.")
                print(f"  Pull it with:  ollama pull {embed_model}")
            print()
        else:
            # sentence-transformers: no endpoint needed
            embed_endpoint = ""
            embed_model = _prompt("Embedding model (HuggingFace model ID)", existing["embed_model"])
            embed_device = _prompt("Device (cpu/cuda/auto)", existing["embed_device"])
            embed_cache_dir = _prompt("HuggingFace cache directory", existing["embed_cache_dir"])
            print(f"  Examples: all-MiniLM-L6-v2, sentence-transformers/all-mpnet-base-v2")
            print()

        # Watch folders
        if existing["folders"]:
            print(f"Current folders: {existing['folders']}")
            change = input("Change folders? [y/N]: ").strip().lower()
            folders = _prompt_folders() if change == "y" else existing["folders"]
        else:
            folders = _prompt_folders()
        print()

    # Write config
    if llm_enabled:
        llm_section = f"""[[llm]]
enabled = true
endpoint = {llm_endpoint!r}
model = {llm_model!r}
batch_size = 5
poll_ms = 3000
max_retries = 3
max_context_chars = 16000
prompt_template = "You are writing documentation for other programmers. Describe the following {{kind}} named \\"{{name}}\\" from {{path}}. Be concise and technical. Do not try and make fixes or note any errors. Do not make guesses, just describe what is in front of you. Limit to 4 sentences. Do not reference this prompt, as you are making a description that is being used in a RAG database.\\n\\n{{context}}\\n"
"""
    else:
        llm_section = "# Descriptions disabled"

    config_content = _CONFIG_TEMPLATE.format(
        folders=_toml_string_list(folders),
        llm_section=llm_section,
        embed_backend=embed_backend,
        embed_endpoint=embed_endpoint,
        embed_model=embed_model,
        embed_device=embed_device,
        embed_cache_dir=embed_cache_dir,
    )
    if dry_run:
        print("-- config.toml (dry run) --")
        print(config_content)
        print("-- secrets.toml (dry run) --")
        print(_SECRETS_TEMPLATE.format(api_key=api_key))
    else:
        config_path.write_text(config_content, encoding="utf-8")
        print(f"  wrote  {config_path}")

        secrets_content = _SECRETS_TEMPLATE.format(api_key=api_key)
        secrets_path.write_text(secrets_content, encoding="utf-8")
        print(f"  wrote  {secrets_path}")

        if not ignore_path.exists():
            ignore_path.write_text(_DEFAULT_IGNORE, encoding="utf-8")
            print(f"  wrote  {ignore_path}")
        else:
            print(f"  skip   {ignore_path}  (already exists)")

    print()

    # Test sentence-transformers and download model if configured
    if not dry_run and embed_backend == "sentence-transformers":
        print("Setting up sentence-transformers embedding model...")
        try:
            import sentence_transformers
            import torch
            print(f"  sentence-transformers {sentence_transformers.__version__} installed")

            # Map "auto" to actual available device
            device_to_use = embed_device
            if device_to_use == "auto":
                device_to_use = "cuda" if torch.cuda.is_available() else "cpu"
                if torch.cuda.is_available():
                    print(f"  Device: auto → cuda (GPU available)")
                else:
                    print(f"  Device: auto → cpu (no GPU)")
            else:
                print(f"  Device: {device_to_use}")

            cache_path = Path(embed_cache_dir).expanduser()
            print(f"  Cache directory: {cache_path}")
            print(f"  Downloading model '{embed_model}'...")
            print(f"  (This may take several minutes on first run)")

            st = sentence_transformers.SentenceTransformer(
                embed_model,
                device=device_to_use,
                cache_folder=str(cache_path)
            )
            print(f"  ✓ Model loaded successfully ({st.get_sentence_embedding_dimension()} dimensions)")
        except ImportError as e:
            print(f"  ✗ sentence-transformers not installed: {e}")
            print(f"  Install with: pip install sentence-transformers")
        except Exception as e:
            print(f"  ✗ Failed to load model: {e}")
        print()

    print("Done. Start blerk with:  blerk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
