from __future__ import annotations

import sys
from pathlib import Path

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

[llm]
endpoint = {llm_endpoint!r}
model = {llm_model!r}
batch_size = 5
poll_ms = 3000
max_retries = 3
max_context_chars = 16000
prompt_template = "You are writing documentation for other programmers. Describe the following {{kind}} named \\"{{name}}\\" from {{path}}. Be concise and technical. Do not try and make fixes or note any errors. Do not make guesses, just describe what is in front of you. Limit to 4 sentences. Do not reference this prompt, as you are making a description that is being used in a RAG database.\\n\\n{{context}}\\n"

[embedder]
endpoint = {embed_endpoint!r}
model = {embed_model!r}
batch_size = 10
poll_ms = 2000
max_retries = 3
max_embed_chars = 2000
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
    inner = ", ".join(f'"{v}"' for v in items)
    return f"[{inner}]"


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

    if config_path.exists():
        ans = input("Config already exists. Reconfigure? [y/N]: ").strip().lower()
        if ans != "y":
            print("Skipping config.")
            return 0
        print()

    # Ollama check
    llm_endpoint = _prompt("LLM endpoint", "http://localhost:11434")
    embed_endpoint = llm_endpoint

    print(f"\nChecking Ollama at {llm_endpoint}...")
    available_models = _check_ollama(llm_endpoint)
    if available_models:
        print(f"  OK — {len(available_models)} model(s) available:")
        for m in available_models:
            print(f"    {m}")
    else:
        print("  Could not reach Ollama. Check that it is running.")
        print("  Continuing with defaults — edit config.toml later if needed.")
    print()

    # LLM model
    llm_model = _prompt("LLM model", "llama3.2")

    # Embed model
    embed_model = _prompt("Embedding model", "nomic-embed-text")
    def _model_available(name: str, models: list[str]) -> bool:
        name_base = name.split(":")[0]
        return any(m == name or m.split(":")[0] == name_base for m in models)

    if available_models and not _model_available(embed_model, available_models):
        print(f"  Warning: '{embed_model}' is not in the available model list.")
        print(f"  Pull it with:  ollama pull {embed_model}")
    print()

    # API key
    api_key = ""
    needs_key = input("Does this endpoint require an API key? [y/N]: ").strip().lower()
    if needs_key == "y":
        api_key = input("API key: ").strip()
    print()

    # Watch folders
    folders = _prompt_folders()
    print()

    # Write config
    config_content = _CONFIG_TEMPLATE.format(
        folders=_toml_string_list(folders),
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
        embed_endpoint=embed_endpoint,
        embed_model=embed_model,
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
    print("Done. Start blerk with:  blerk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
