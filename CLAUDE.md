## Important

- when tagging for a new release, make sure to prefix version number with "v", eg "v1.0.1" and update pyproject.toml to match.

## Paths

There is exactly one way to handle a path in this codebase. Do not add a second.

- **Never write a directory filter by hand.** Build a `Scope` and call `scope_filters` or `scope_clause` from `blerk_cmd/util.py`. Pass the column name when the query uses a join alias. This rule exists because there were once eleven hand-rolled filters and no two agreed.
- **Never normalize a path inline.** Use `to_slash` (no filesystem access) or `resolve_path` (resolves when the path exists) from `blerk/paths.py`.
- **`blerk/paths.py` must not import from `blerk_cmd`.** `blerk/config.py` and `blerk/ignore_match.py` depend on it.
- **`file_paths` is the only table with a `path` column.** `files` is content-addressed and holds just the hash and size. Joining `files` and filtering `f.path` is a runtime error, and has been shipped twice.
- **Stored paths are absolute with forward slashes.** Tests that seed the database must do the same, or they will pass against matchers that real data would fail.
- **Directory arguments are optional and repo-relative.** A relative directory joins to the index root, never to the process working directory. Results print relative to that root.

See the "Paths and scope" section of ARCHITECTURE.md for how the readings widen and why.

## Embeddings

- **blerk never loads model weights.** No torch, no sentence-transformers, no device or cache settings. Every embedding is an HTTP POST to an OpenAI-compatible `/v1/embeddings` endpoint, built only in `blerk/embedding.py`.
- **One protocol.** The describer and reranker use `/v1/chat/completions`, the embedder uses `/v1/embeddings`. Do not add a provider-native call path.
- **Hardware placement is the server's job.** If someone wants CPU embeddings, that is an Ollama Modelfile with `num_gpu 0`, not a blerk setting.
- **Never let an embedding failure degrade silently.** A wrong or missing vector space produces plausible rankings that are quietly wrong, so unset, unreachable and error responses must all name the endpoint and the fix.

## Daemons

- **`blerk start` is one process.** Every daemon is a supervised thread. Do not reintroduce subprocesses: shutdown cannot be delivered gracefully on Windows, and the previous design leaked 94 orphaned daemons in a single session.
- **A new daemon exposes `run(cfg, shutdown, ...)`** taking a `threading.Event`, and the hub runs it through `supervise`. That is the only way to add one.
- **`db._write_lock` now actually serializes writes.** It always looked like it did; with one process it does. Assume contention is real rather than absorbed by `busy_timeout`.

## Tests

`uv run --with pytest python -m pytest -q` from the repo root. There is no committed lockfile, so uv resolves on first run.

## Installing while developing

`blerk` is installed with **pipx**, so the running daemons use the installed copy, not the working tree.

- **`pipx install --force .` does not reliably update the code.** It prints success and exits 0, but pip skips the reinstall when the version has not changed. Use `pipx uninstall blerk && pipx install .`.
- **Verify from outside the repo.** Running a check from the repo root imports the working tree from the current directory rather than the installed package, so a stale install looks correct.
- **`pipx install --force`** also reuses the venv, so removed dependencies stay. A clean reinstall is what reclaims the space (dropping torch took the venv from 4593 MB to 57 MB).
