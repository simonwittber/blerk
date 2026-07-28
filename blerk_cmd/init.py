from __future__ import annotations

import sys
from pathlib import Path

_BLERK_DIR = Path.home() / ".blerk"

_DEFAULT_CONFIG = """\
# Path to secrets file. Keep this out of version control.
secrets_file = "~/.blerk/secrets.toml"

[db]
path = "~/.blerk/blerk.db"

[watch]
# Add the folders you want indexed.
folders = []
debounce_ms = 100
ignore_file = "~/.blerk/ignore"

[symbolizer]
# "regexp" is fast but no call graph. "treesitter" is accurate and extracts callers/callees.
engine = "regexp"
batch_size = 10
poll_ms = 1000
max_retries = 3
min_describe_lines = 5

[git_enricher]
batch_size = 20
poll_ms = 2000
max_retries = 3

[llm]
# Any OpenAI-compatible endpoint works (Ollama, OpenAI, etc.).
endpoint = "http://localhost:11434"
model = "llama3.2"
batch_size = 5
poll_ms = 3000
max_retries = 3
max_context_chars = 16000
prompt_template = \"\"\"Describe the following {kind} named "{name}" from {path}. Be concise and technical.

{context}\"\"\"

[embedder]
# Must support Ollama's native /api/embeddings format.
endpoint = "http://localhost:11434"
model = "nomic-embed-text"
batch_size = 10
poll_ms = 2000
max_retries = 3
# nomic-embed-text has an 8192-token context; ~2000 chars is a safe ceiling for dense code.
max_embed_chars = 2000
"""

_DEFAULT_SECRETS = """\
# LLM API key. Required only if your endpoint enforces authentication.
[llm]
api_key = ""
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
packages/
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
Temp/
Logs/
UserSettings/
*.meta

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


def _write_if_absent(path: Path, content: str, label: str) -> bool:
    if path.exists():
        print(f"  skip    {path}  (already exists)")
        return False
    path.write_text(content, encoding="utf-8")
    print(f"  created {path}")
    return True


def main(argv: list[str] | None = None) -> int:
    _BLERK_DIR.mkdir(parents=True, exist_ok=True)
    print(f"blerk init: {_BLERK_DIR}")
    print()

    _write_if_absent(_BLERK_DIR / "config.toml", _DEFAULT_CONFIG, "config")
    _write_if_absent(_BLERK_DIR / "secrets.toml", _DEFAULT_SECRETS, "secrets")
    _write_if_absent(_BLERK_DIR / "ignore", _DEFAULT_IGNORE, "ignore")

    print()
    print("Next steps:")
    print(f"  1. Edit {_BLERK_DIR / 'config.toml'} and add your folders under [watch].")
    print( "  2. Pull the embedding model:  ollama pull nomic-embed-text")
    print( "  3. Start the hub:             blerk")
    print( "  4. Query:                     blerk-query \"how does X work\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
