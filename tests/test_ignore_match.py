from __future__ import annotations

import os
from pathlib import Path

import pytest

from blerk.ignore_match import (
    IgnoreSet,
    Kind,
    Pattern,
    compile_pattern,
    glob_to_regex,
    is_ignored,
    load_ignore_file,
)


def test_compile_pattern_exact():
    p = compile_pattern("foo.txt", False, False)
    assert p.kind == Kind.EXACT
    assert p.value == "foo.txt"


def test_compile_pattern_suffix():
    p = compile_pattern("*.pyc", False, False)
    assert p.kind == Kind.SUFFIX
    assert p.value == ".pyc"


def test_compile_pattern_prefix():
    p = compile_pattern("build*", False, False)
    assert p.kind == Kind.PREFIX
    assert p.value == "build"


def test_compile_pattern_contains():
    p = compile_pattern("*node*", False, False)
    assert p.kind == Kind.CONTAINS
    assert p.value == "node"


def test_compile_pattern_regexp_from_double_star():
    p = compile_pattern("a/**/b", False, True)
    assert p.kind == Kind.REGEXP
    assert p.re is not None


def test_compile_pattern_regexp_from_question():
    p = compile_pattern("foo?.txt", False, False)
    assert p.kind == Kind.REGEXP


def test_compile_pattern_regexp_from_bracket():
    p = compile_pattern("[abc].txt", False, False)
    assert p.kind == Kind.REGEXP


def test_compile_pattern_lowercases_value():
    p = compile_pattern("FOO.TXT", False, False)
    assert p.value == "foo.txt"


def test_compile_pattern_flags():
    p = compile_pattern("foo", True, True)
    assert p.dir_only is True
    assert p.has_slash is True


def test_glob_to_regex_double_star():
    assert glob_to_regex("**") == "^.*$"


def test_glob_to_regex_single_star():
    assert glob_to_regex("*") == "^[^/]*$"


def test_glob_to_regex_question():
    assert glob_to_regex("?") == "^[^/]$"


def test_glob_to_regex_literal_dot():
    assert glob_to_regex(".") == "^\\.$"


def test_glob_to_regex_mixed():
    assert glob_to_regex("a/**/b.*") == "^a/.*/b\\.[^/]*$"


def test_glob_to_regex_escapes_specials():
    assert glob_to_regex("a+b(c)") == "^a\\+b\\(c\\)$"


def test_load_ignore_file_skips_blanks_comments_negations(tmp_path: Path):
    p = tmp_path / "ig"
    p.write_text(
        "\n"
        "# comment line\n"
        "  \n"
        "!negation.txt\n"
        "foo.txt\n"
        "build/\n"
        "*.log\n",
        encoding="utf-8",
    )
    patterns = load_ignore_file(str(p))
    assert len(patterns) == 3
    assert patterns[0].value == "foo.txt"
    assert patterns[1].value == "build"
    assert patterns[1].dir_only is True
    assert patterns[2].value == ".log"
    assert patterns[2].kind == Kind.SUFFIX


def test_load_ignore_file_has_slash(tmp_path: Path):
    p = tmp_path / "ig"
    p.write_text("src/foo.txt\nplain.txt\n", encoding="utf-8")
    patterns = load_ignore_file(str(p))
    assert patterns[0].has_slash is True
    assert patterns[1].has_slash is False


def test_is_ignored_empty_sets():
    assert is_ignored("/a/b/c.txt", False, []) is False


def test_is_ignored_by_name_suffix(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("*.pyc", False, False)])]
    assert is_ignored(os.path.join(root, "foo.pyc"), False, sets) is True
    assert is_ignored(os.path.join(root, "foo.py"), False, sets) is False


def test_is_ignored_case_insensitive(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("Foo.TXT", False, False)])]
    assert is_ignored(os.path.join(root, "FOO.txt"), False, sets) is True
    assert is_ignored(os.path.join(root, "foo.txt"), False, sets) is True


def test_is_ignored_dir_only(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("build", True, False)])]
    build_path = os.path.join(root, "build")
    assert is_ignored(build_path, True, sets) is True
    assert is_ignored(build_path, False, sets) is False


def test_is_ignored_file_inside_ignored_dir(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("build", True, False)])]
    assert is_ignored(os.path.join(root, "build", "output.cs"), False, sets) is True
    assert is_ignored(os.path.join(root, "build", "sub", "deep.cs"), False, sets) is True
    assert is_ignored(os.path.join(root, "src", "main.cs"), False, sets) is False


def test_is_ignored_has_slash_matches_rel(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("src/foo.txt", False, True)])]
    assert is_ignored(os.path.join(root, "src", "foo.txt"), False, sets) is True
    assert is_ignored(os.path.join(root, "foo.txt"), False, sets) is False


def test_is_ignored_no_slash_matches_basename(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("foo.txt", False, False)])]
    assert is_ignored(os.path.join(root, "any", "nested", "foo.txt"), False, sets) is True


def test_is_ignored_windows_backslash_normalized(tmp_path: Path):
    root = str(tmp_path)
    sets = [IgnoreSet(dir=root, patterns=[compile_pattern("a/b.txt", False, True)])]
    nested = os.path.join(root, "a", "b.txt")
    assert is_ignored(nested, False, sets) is True


def test_is_ignored_top_level_blerk_ignore():
    ignore_path = str(Path(__file__).resolve().parent.parent / "ignore")
    if not os.path.exists(ignore_path):
        pytest.skip("top-level ignore file not present")
    patterns = load_ignore_file(ignore_path)
    root = str(Path(__file__).resolve().parent.parent)
    sets = [IgnoreSet(dir=root, patterns=patterns)]

    assert is_ignored(os.path.join(root, "node_modules"), True, sets) is True
    assert is_ignored(os.path.join(root, "node_modules", "foo"), False, sets) is True
    assert is_ignored(os.path.join(root, ".git"), True, sets) is True
    assert is_ignored(os.path.join(root, "foo.pyc"), False, sets) is True
    assert is_ignored(os.path.join(root, "app.log"), False, sets) is True
    assert is_ignored(os.path.join(root, "Library"), True, sets) is True
    assert is_ignored(os.path.join(root, "src", "main.py"), False, sets) is False
