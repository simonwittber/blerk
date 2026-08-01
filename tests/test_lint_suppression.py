from __future__ import annotations

from blerk_cmd.lint import load_suppressions, _is_suppressed, lint
from blerk_cmd.lint_rules import build_scope, long_function


def _insert_file(conn, path: str) -> int:
    cur = conn.execute("INSERT INTO files(path, mtime, hash) VALUES(?,0,'h')", (path.replace("\\", "/"),))
    return int(cur.lastrowid)


def _insert_long_fn(conn, file_id: int, name: str, start: int = 1, length: int = 50) -> int:
    cur = conn.execute(
        "INSERT INTO symbols(file_id, name, kind, line, end_line) VALUES(?,?,?,?,?)",
        (file_id, name, "function", start, start + length),
    )
    return int(cur.lastrowid)


def _write_blerk(tmp_path, subdir: str, content: str) -> None:
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / ".blerk").write_text(content, encoding="utf-8")


class TestLoadSuppressions:
    def test_suppress_loaded(self, tmp_path):
        _write_blerk(tmp_path, "src", 'suppress = ["long_function"]')
        sups = load_suppressions(str(tmp_path))
        assert len(sups) == 1
        assert "long_function" in sups[0].rules

    def test_exclude_loaded_as_absolute_pattern(self, tmp_path):
        _write_blerk(tmp_path, "src", 'exclude = ["*.generated.py"]')
        sups = load_suppressions(str(tmp_path))
        assert len(sups) == 1
        assert len(sups[0].excludes) == 1
        assert sups[0].excludes[0].endswith("/src/*.generated.py")

    def test_empty_file_ignored(self, tmp_path):
        _write_blerk(tmp_path, "src", "")
        sups = load_suppressions(str(tmp_path))
        assert not sups

    def test_invalid_toml_ignored(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        (d / ".blerk").write_bytes(b"\xff\xfe")
        sups = load_suppressions(str(tmp_path))
        assert not sups


class TestIsSupressed:
    def test_exact_dir_match(self, tmp_path):
        _write_blerk(tmp_path, "src", 'suppress = ["long_function"]')
        sups = load_suppressions(str(tmp_path))
        src = str(tmp_path / "src").replace("\\", "/")
        assert _is_suppressed(f"{src}/foo.py", "long_function", sups)

    def test_subdir_match(self, tmp_path):
        _write_blerk(tmp_path, "src", 'suppress = ["long_function"]')
        sups = load_suppressions(str(tmp_path))
        src = str(tmp_path / "src").replace("\\", "/")
        assert _is_suppressed(f"{src}/sub/foo.py", "long_function", sups)

    def test_sibling_dir_not_matched(self, tmp_path):
        _write_blerk(tmp_path, "src", 'suppress = ["long_function"]')
        sups = load_suppressions(str(tmp_path))
        other = str(tmp_path / "other").replace("\\", "/")
        assert not _is_suppressed(f"{other}/foo.py", "long_function", sups)

    def test_wildcard_suppresses_all_rules(self, tmp_path):
        _write_blerk(tmp_path, "src", 'suppress = ["*"]')
        sups = load_suppressions(str(tmp_path))
        src = str(tmp_path / "src").replace("\\", "/")
        assert _is_suppressed(f"{src}/foo.py", "any_rule", sups)


class TestBlerkExclude:
    def test_excluded_files_not_linted(self, conn, tmp_path):
        gen_dir = tmp_path / "src"
        gen_dir.mkdir()
        (gen_dir / ".blerk").write_text('exclude = ["*.generated.py"]', encoding="utf-8")

        gen_path = str(tmp_path / "src" / "foo.generated.py")
        normal_path = str(tmp_path / "src" / "bar.py")

        fid_gen = _insert_file(conn, gen_path)
        fid_norm = _insert_file(conn, normal_path)
        _insert_long_fn(conn, fid_gen, "gen_fn")
        _insert_long_fn(conn, fid_norm, "norm_fn")

        thresholds = {"long_function": 40}
        violations = lint(conn, str(tmp_path), thresholds)
        paths = {v[0] for v in violations}
        assert not any("generated" in p for p in paths)
        assert any("bar.py" in p for p in paths)

    def test_exclude_does_not_affect_other_dirs(self, conn, tmp_path):
        src = tmp_path / "src"
        other = tmp_path / "other"
        src.mkdir()
        other.mkdir()
        (src / ".blerk").write_text('exclude = ["*.generated.py"]', encoding="utf-8")

        gen_other = str(other / "foo.generated.py")
        fid = _insert_file(conn, gen_other)
        _insert_long_fn(conn, fid, "fn")

        thresholds = {"long_function": 40}
        violations = lint(conn, str(tmp_path), thresholds)
        paths = {v[0] for v in violations}
        assert any("other" in p for p in paths)
