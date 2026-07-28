from __future__ import annotations

import os

import pytest

from blerk.symbols.regexp_extractor import (
    extract_symbols,
    find_end_brace,
    find_end_heading,
    find_end_indent,
)
from blerk.symbols.types import (
    EXT_TO_LANG,
    MARKER_END,
    MARKER_START,
    SNIPPET_MAX_LINES,
    build_context,
)


def write_temp(tmp_path, ext: str, content: str) -> str:
    p = tmp_path / ("f" + ext)
    p.write_bytes(content.encode("utf-8"))
    return str(p)


def find_sym(syms, name, kind):
    for s in syms:
        if s.name == name and s.kind == kind:
            return s
    return None


def test_ext_to_lang_supported():
    supported = [".cs", ".go", ".js", ".jsx", ".py", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".md", ".markdown"]
    for ext in supported:
        assert ext in EXT_TO_LANG, f"missing {ext}"


def test_ext_to_lang_unsupported():
    for ext in [".txt", ".json", ".xml"]:
        assert ext not in EXT_TO_LANG


def test_extract_symbols_go(tmp_path):
    src = "\npackage example\n\ntype MyType struct{}\n\nfunc MyFunc() {}\n"
    path = write_temp(tmp_path, ".go", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "MyFunc", "function") is not None
    assert find_sym(syms, "MyType", "type") is not None


def test_extract_symbols_csharp(tmp_path):
    src = "using System;\n\nnamespace Example\n{\n    public class MyClass\n    {\n        public void MyMethod()\n        {\n        }\n    }\n}\n"
    path = write_temp(tmp_path, ".cs", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "MyClass", "class") is not None
    assert find_sym(syms, "MyMethod", "method") is not None


def test_extract_symbols_python(tmp_path):
    src = "def top_fn(x):\n    return x\n\nclass Foo:\n    def bar(self):\n        return 1\n"
    path = write_temp(tmp_path, ".py", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "top_fn", "function") is not None
    assert find_sym(syms, "Foo", "class") is not None
    assert find_sym(syms, "bar", "method") is not None


def test_extract_symbols_js(tmp_path):
    src = "function foo() {\n  return 1;\n}\n\nexport class Bar {\n  async baz() {\n    return 2;\n  }\n}\n"
    path = write_temp(tmp_path, ".js", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "foo", "function") is not None
    assert find_sym(syms, "Bar", "class") is not None
    assert find_sym(syms, "baz", "method") is not None


def test_extract_symbols_ts_mapped(tmp_path):
    src = "export function tsFunc(): void {\n  return;\n}\n"
    path = write_temp(tmp_path, ".ts", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "tsFunc", "function") is not None


def test_extract_symbols_c(tmp_path):
    src = "int foo(int x) {\n    return x;\n}\n"
    path = write_temp(tmp_path, ".c", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "foo", "function") is not None


def test_extract_symbols_cpp(tmp_path):
    src = "class MyCppClass {\n  public:\n    int val;\n};\n\nvoid myFunc(int x) {\n    return;\n}\n"
    path = write_temp(tmp_path, ".cpp", src)
    syms = extract_symbols(path)
    assert find_sym(syms, "MyCppClass", "class") is not None
    assert find_sym(syms, "myFunc", "function") is not None


def test_extract_symbols_markdown_headings(tmp_path):
    src = "# A\ncontent a\n## B\ncontent b\n## C\ncontent c\n# D\ncontent d\n"
    path = write_temp(tmp_path, ".md", src)
    syms = extract_symbols(path)
    a = find_sym(syms, "A", "heading")
    b = find_sym(syms, "B", "heading")
    c = find_sym(syms, "C", "heading")
    d = find_sym(syms, "D", "heading")
    assert a and b and c and d
    assert (a.line, a.end_line) == (1, 6)
    assert (b.line, b.end_line) == (3, 4)
    assert (c.line, c.end_line) == (5, 6)
    assert d.line == 7


def test_extract_symbols_unsupported_ext(tmp_path):
    p = write_temp(tmp_path, ".txt", "hello\n")
    assert extract_symbols(p) == []


def test_extract_symbols_missing_file(tmp_path):
    missing = str(tmp_path / "nope.go")
    assert extract_symbols(missing) == []


def test_find_end_brace_nested():
    lines = [
        "func Foo() {",
        "    if x {",
        "        y()",
        "    }",
        "}",
        "trailing",
    ]
    assert find_end_brace(lines, 0) == 5


def test_find_end_brace_cap_exceeded():
    lines = ["func Foo() {"] + ["    body"] * 199
    assert find_end_brace(lines, 0) == SNIPPET_MAX_LINES


def test_find_end_brace_no_braces():
    lines = ["signature only", "another line"]
    assert find_end_brace(lines, 0) == len(lines)


def test_find_end_indent_python_blank_lines():
    lines = [
        "def foo():",
        "    a = 1",
        "",
        "    b = 2",
        "next_thing()",
    ]
    assert find_end_indent(lines, 0) == 4


def test_find_end_heading_top_level():
    lines = ["# A", "content", "## B", "content", "# C"]
    assert find_end_heading(lines, 0) == 4
    assert find_end_heading(lines, 2) == 4


def test_build_context_small_file(tmp_path):
    src = "package example\n\nfunc Alpha() {\n\treturn\n}\n\nfunc Beta() {\n\treturn\n}\n"
    path = write_temp(tmp_path, ".go", src)
    ctx = build_context(path, 3, 5, 100000)
    assert MARKER_START in ctx
    assert MARKER_END in ctx
    assert "func Beta()" in ctx


def test_build_context_large_file(tmp_path):
    src = "package example\n\nfunc Alpha() {\n\t// alpha body\n\treturn\n}\n\nfunc Beta() {\n\t// beta body\n\treturn\n}\n"
    path = write_temp(tmp_path, ".go", src)
    ctx = build_context(path, 3, 6, 1)
    assert MARKER_START in ctx
    assert "func Alpha()" in ctx
    assert "// alpha body" in ctx
    assert "// beta body" not in ctx
    assert "// ..." in ctx


def test_build_context_missing_file(tmp_path):
    missing = str(tmp_path / "nope.go")
    with pytest.raises(OSError):
        build_context(missing, 1, 1, 100)


def test_build_context_stripped_preserves_target(tmp_path):
    src = (
        "package example\n\nfunc Alpha() {\n\t// alpha body\n\treturn\n}\n\n"
        "func Beta() {\n\t// beta body\n\treturn\n}\n\n"
        "func Gamma() {\n\t// gamma body\n\treturn\n}\n"
    )
    path = write_temp(tmp_path, ".go", src)
    ctx = build_context(path, 8, 11, 1)
    assert "// beta body" in ctx
    assert "// alpha body" not in ctx
    assert "// gamma body" not in ctx


def test_build_context_contains_target_kept(tmp_path):
    src = "class Foo:\n    def bar(self):\n        return 1\n    def baz(self):\n        return 2\n    def qux(self):\n        return 3\n"
    path = write_temp(tmp_path, ".py", src)
    ctx = build_context(path, 2, 3, 1)
    assert "return 1" in ctx
    assert "class Foo:" in ctx
    assert "return 2" not in ctx
    assert "return 3" not in ctx
