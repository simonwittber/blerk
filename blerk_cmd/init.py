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
endpoint = {embed_endpoint}
model = {embed_model}
batch_size = 10
poll_ms = 2000
max_retries = 3
max_embed_chars = 2000

[reranker]
enabled = {reranker_enabled}
endpoint = {reranker_endpoint}
model = {reranker_model}
api_key = {reranker_api_key}
"""

_SECRETS_TEMPLATE = """\
[llm]
api_key = {api_key}
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


def _prompt_choice(message: str, options: list[tuple[str, str]], default_idx: int = 0) -> str:
    """Prompt user to select from a list of options.

    options: list of (value, description) tuples
    default_idx: index of default option
    Returns: the selected value
    """
    print(f"\n{message}")
    for i, (val, desc) in enumerate(options, 1):
        mark = " (default)" if i == default_idx + 1 else ""
        print(f"  {i}. {val}{mark}")
        print(f"     {desc}")

    while True:
        choice = input(f"Select [1-{len(options)}] ({default_idx + 1}): ").strip()
        if not choice:
            return options[default_idx][0]
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print(f"Invalid choice. Please enter a number between 1 and {len(options)}.")


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


def _toml_string(value: str) -> str:
    """Quote a value as a TOML basic string.
    Python's repr() is not usable here: it emits a single-quoted literal that TOML reads without escape processing, so backslashes end up doubled.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    """TOML booleans are lowercase, unlike Python's True/False."""
    return "true" if value else "false"


def _toml_string_list(items: list[str]) -> str:
    inner = ", ".join(_toml_string(v) for v in items)
    return f"[{inner}]"


def _load_existing_config(config_path: Path) -> dict:
    """Load existing config and extract current values as defaults."""
    defaults = {
        "folders": [],
        "llm_enabled": True,
        "llm_endpoint": "http://localhost:11434",
        "llm_model": "llama3.2",
        "embed_endpoint": "http://localhost:11434",
        "embed_model": "nomic-embed-text",
        "reranker_enabled": False,
        "reranker_endpoint": "http://localhost:11434",
        "reranker_model": "",
        "reranker_api_key": "",
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
            defaults["embed_endpoint"] = embed.get("endpoint", defaults["embed_endpoint"])
            defaults["embed_model"] = embed.get("model", defaults["embed_model"])
        if "reranker" in cfg:
            rr = cfg["reranker"]
            defaults["reranker_enabled"] = rr.get("enabled", defaults["reranker_enabled"])
            defaults["reranker_endpoint"] = rr.get("endpoint", defaults["reranker_endpoint"])
            defaults["reranker_model"] = rr.get("model", defaults["reranker_model"])
            defaults["reranker_api_key"] = rr.get("api_key", defaults["reranker_api_key"])
    except Exception as e:
        print(f"Warning: could not parse existing config: {e}")
    return defaults


def _detect_embedding_model_change(existing: dict, new_model: str, db_path: str) -> bool:
    """Detect if the embedding model changed. If so, ask to re-queue."""
    old_model = existing.get("embed_model", "nomic-embed-text")

    if old_model == new_model:
        return False  # No change

    print("⚠ Embedding model changed!")
    print(f"  Old: {old_model}")
    print(f"  New: {new_model}")
    print()

    ans = input("Re-queue all blocks for re-embedding? [y/N]: ").strip().lower()
    if ans != "y":
        print("Skipping re-embedding. Old embeddings will remain (but won't be used).")
        return False

    # Re-queue everything
    try:
        from blerk import db as blerk_db
        conn = blerk_db.open_db(db_path)
        conn.execute(
            "UPDATE code_block_embed_queue SET status='pending', priority=1 WHERE status IN ('completed', 'failed')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO code_block_embed_queue(block_id, status, priority) "
            "SELECT id, 'pending', 1 FROM code_blocks "
            "WHERE id NOT IN (SELECT block_id FROM code_block_embed_queue)"
        )
        conn.commit()
        conn.close()
        print("✓ All blocks queued for re-embedding")
    except Exception as e:
        print(f"✗ Failed to queue blocks: {e}")
    print()
    return True


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
    analyzers_path = _BLERK_DIR / "analyzers.toml"
    _ANALYZERS_EXAMPLE = Path(__file__).parent.parent / "analyzers.example.toml"

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
        embed_endpoint = existing["embed_endpoint"]
        embed_model = existing["embed_model"]
        api_key = ""
        folders = existing["folders"]
        available_models = []
        reranker_enabled = existing["reranker_enabled"]
        reranker_endpoint = existing["reranker_endpoint"]
        reranker_model = existing["reranker_model"]
        reranker_api_key = existing["reranker_api_key"]
    else:
        # Enable descriptions?
        enable_llm = input("Enable code descriptions? [Y/n]: ").strip().lower()
        llm_enabled = enable_llm != "n"
        print()

        # Embedding endpoint. blerk never loads model weights itself, so a running server is required.
        available_models = []
        embed_endpoint = _prompt("Embedding endpoint (OpenAI-compatible, e.g. Ollama)", existing["embed_endpoint"])
        print(f"\nChecking {embed_endpoint}...")
        available_models = _check_ollama(embed_endpoint)
        if available_models:
            print(f"  OK, {len(available_models)} model(s) available:")
            for m in available_models:
                print(f"    {m}")
        else:
            print("  Could not reach the endpoint. Check that it is running.")
            print("  Continuing with defaults. Edit config.toml later if needed.")
        print()

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

        # Embedding model
        embed_models = [
            ("nomic-embed-text", "Fast, widely used (768 dims)"),
            ("mxbai-embed-large", "Larger, higher quality (1024 dims)"),
        ]
        default_embed_idx = 0 if existing["embed_model"] != "mxbai-embed-large" else 1
        embed_model = _prompt_choice("Select embedding model:", embed_models, default_embed_idx)
        if embed_model not in available_models and available_models:
            print(f"  Not pulled yet. Run: ollama pull {embed_model}")
        print()

        # Reranker
        enable_rr = input("Enable reranker (re-ranks query results via LLM)? [y/N]: ").strip().lower()
        reranker_enabled = enable_rr == "y"
        if reranker_enabled:
            reranker_endpoint = _prompt("Reranker endpoint", existing["reranker_endpoint"])
            reranker_model = _prompt("Reranker model", existing["reranker_model"])
            needs_rr_key = input("Does the reranker endpoint require an API key? [y/N]: ").strip().lower()
            reranker_api_key = input("API key: ").strip() if needs_rr_key == "y" else ""
        else:
            reranker_endpoint = existing["reranker_endpoint"]
            reranker_model = existing["reranker_model"]
            reranker_api_key = existing["reranker_api_key"]
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
endpoint = {_toml_string(llm_endpoint)}
model = {_toml_string(llm_model)}
batch_size = 5
poll_ms = 3000
max_retries = 3
max_context_chars = 16000
prompt_template = "You are writing documentation for other programmers. Describe the following {{kind}} named \\"{{name}}\\" from {{path}}. Be concise and technical. Do not try and make fixes or note any errors. Do not make guesses, just describe what is in front of you. Limit to 4 sentences. Do not reference this prompt, as you are making a description that is being used in a RAG database.\\n\\n{{context}}\\n"
"""
    else:
        llm_section = "[[llm]]\nenabled = false\n"

    config_content = _CONFIG_TEMPLATE.format(
        folders=_toml_string_list(folders),
        llm_section=llm_section,
        embed_endpoint=_toml_string(embed_endpoint),
        embed_model=_toml_string(embed_model),
        reranker_enabled=_toml_bool(reranker_enabled),
        reranker_endpoint=_toml_string(reranker_endpoint),
        reranker_model=_toml_string(reranker_model),
        reranker_api_key=_toml_string(reranker_api_key),
    )
    if dry_run:
        print("-- config.toml (dry run) --")
        print(config_content)
        print("-- secrets.toml (dry run) --")
        print(_SECRETS_TEMPLATE.format(api_key=_toml_string(api_key)))
    else:
        config_path.write_text(config_content, encoding="utf-8")
        print(f"  wrote  {config_path}")

        secrets_content = _SECRETS_TEMPLATE.format(api_key=_toml_string(api_key))
        secrets_path.write_text(secrets_content, encoding="utf-8")
        print(f"  wrote  {secrets_path}")

        if not ignore_path.exists():
            ignore_path.write_text(_DEFAULT_IGNORE, encoding="utf-8")
            print(f"  wrote  {ignore_path}")
        else:
            print(f"  skip   {ignore_path}  (already exists)")

        if not analyzers_path.exists():
            if _ANALYZERS_EXAMPLE.exists():
                import shutil
                shutil.copy(_ANALYZERS_EXAMPLE, analyzers_path)
                print(f"  wrote  {analyzers_path}")
            else:
                print(f"  skip   {analyzers_path}  (analyzers.example.toml not found)")
        else:
            print(f"  skip   {analyzers_path}  (already exists)")

        print()

        # Detect if embedding model changed and offer to re-queue
        # Only check if DB already exists (not a fresh install)
        db_path = str(Path("~/.blerk/blerk.db").expanduser())
        if Path(db_path).exists():
            _detect_embedding_model_change(existing, embed_model, db_path)

    print()

    # Verify the endpoint can actually embed with the chosen model, since nothing else will until a search runs.
    if not dry_run:
        print(f"Testing embeddings with '{embed_model}'...")
        try:
            from blerk import embedding
            vec = embedding.embed(embed_endpoint, embed_model, "hello", api_key)
            print(f"  OK, {len(vec)} dimensions")
            if len(vec) != 768:
                print(f"  Note: set vector_dim = {len(vec)} in config.toml")
        except Exception as e:
            print(f"  Failed: {e}")
            print(f"  If the model is missing, run: ollama pull {embed_model}")
        print()

    print("Done. Start blerk with:  blerk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
