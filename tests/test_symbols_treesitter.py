from __future__ import annotations

from blerk.symbols.treesitter_extractor import Extractor


def write_temp(tmp_path, name: str, src: str) -> str:
    p = tmp_path / name
    p.write_bytes(src.encode("utf-8"))
    return str(p)


def symbol_names(syms):
    return [s.name for s in syms]


def test_go_functions(tmp_path):
    src = "package main\n\nfunc Foo(x int) int { return x }\n\nfunc Bar() {}\n"
    path = write_temp(tmp_path, "a.go", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "main.Foo" in names and "main.Bar" in names
    for s in syms:
        assert s.kind in ("function", "method", "type")


def test_go_no_keyword_symbols(tmp_path):
    src = (
        "package main\n\nfunc Run() {\n\tfor i := 0; i < 10; i++ {\n"
        "\t\tif i > 5 {\n\t\t\treturn\n\t\t}\n\t}\n}\n"
    )
    path = write_temp(tmp_path, "b.go", src)
    syms, _ = Extractor().extract(path)
    for s in syms:
        assert s.name not in ("for", "if", "return", "range")


def test_go_call_refs(tmp_path):
    src = "package main\n\nfunc Alpha() {\n\tBeta()\n}\n\nfunc Beta() {}\n"
    path = write_temp(tmp_path, "c.go", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "main.Alpha" and r.callee_name == "main.Beta" for r in refs)


def test_python_class_and_method(tmp_path):
    src = "class MyClass:\n    def my_method(self):\n        pass\n\ndef top_level():\n    pass\n"
    path = write_temp(tmp_path, "d.py", src)
    syms, _ = Extractor().extract(path)
    kinds = {s.name: s.kind for s in syms}
    assert any(k.endswith("MyClass") and v == "class" for k, v in kinds.items())
    assert any(k.endswith("MyClass.my_method") and v == "method" for k, v in kinds.items())
    assert any(k.endswith("top_level") and v == "function" for k, v in kinds.items())


def test_js_function_and_class(tmp_path):
    src = "function greet(name) { return name; }\n\nclass Animal {\n  speak() { return \"\"; }\n}\n"
    path = write_temp(tmp_path, "e.js", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "greet" in names
    assert "Animal" in names


def test_cs_class_and_method(tmp_path):
    src = "public class Greeter {\n    public void Hello() {}\n}\n"
    path = write_temp(tmp_path, "f.cs", src)
    syms, _ = Extractor().extract(path)
    kinds = {s.name: s.kind for s in syms}
    assert kinds.get("Greeter") == "class"
    assert kinds.get("Greeter.Hello") == "method"


def test_markdown_fallback(tmp_path):
    src = "# Heading\n\nSome text.\n"
    path = write_temp(tmp_path, "g.md", src)
    syms, refs = Extractor().extract(path)
    assert refs == []
    assert any(s.kind == "heading" and s.name == "Heading" for s in syms)


def test_line_numbers(tmp_path):
    src = "package main\n\nfunc First() {}\n\nfunc Second() {}\n"
    path = write_temp(tmp_path, "h.go", src)
    syms, _ = Extractor().extract(path)
    line_of = {s.name: s.line for s in syms}
    assert line_of.get("main.First") == 3
    assert line_of.get("main.Second") == 5


def test_snippet_cap(tmp_path):
    body = "\n".join([f"    x{i} = {i}" for i in range(200)])
    src = f"def big():\n{body}\n"
    path = write_temp(tmp_path, "big.py", src)
    syms, _ = Extractor().extract(path)
    big = next(s for s in syms if s.short_name == "big")
    assert big.snippet.count("\n") + 1 <= 100


def test_unsupported_ext_delegates(tmp_path):
    src = "# heading\ncontent\n"
    path = write_temp(tmp_path, "x.md", src)
    syms, refs = Extractor().extract(path)
    assert refs == []
    assert any(s.name == "heading" for s in syms)


# ---------------------------------------------------------------------------
# Qualified name tests
# These assert that symbol names include namespace/class context.
# ---------------------------------------------------------------------------

def test_cs_qualified_name_namespace_and_class(tmp_path):
    src = (
        "namespace MyApp.Core {\n"
        "    public class Processor {\n"
        "        public void Execute() {}\n"
        "    }\n"
        "}\n"
    )
    path = write_temp(tmp_path, "q.cs", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "MyApp.Core.Processor.Execute" in names


def test_cs_qualified_name_class_only(tmp_path):
    src = (
        "public class Greeter {\n"
        "    public void Hello() {}\n"
        "}\n"
    )
    path = write_temp(tmp_path, "r.cs", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "Greeter.Hello" in names


def test_py_qualified_name_class_and_method(tmp_path):
    src = (
        "class MyService:\n"
        "    def process(self):\n"
        "        pass\n"
    )
    path = write_temp(tmp_path, "s.py", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert any("MyService.process" in n for n in names)


def test_go_qualified_name_package_and_type(tmp_path):
    src = (
        "package core\n\n"
        "type Worker struct{}\n\n"
        "func (w Worker) Run() {}\n"
    )
    path = write_temp(tmp_path, "t.go", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "core.Worker.Run" in names


def test_call_refs_use_short_names(tmp_path):
    src = (
        "namespace App {\n"
        "    public class Foo {\n"
        "        public void Alpha() { Beta(); }\n"
        "        public void Beta() {}\n"
        "    }\n"
        "}\n"
    )
    path = write_temp(tmp_path, "u.cs", src)
    _, refs = Extractor().extract(path)
    caller_names = {r.caller_name for r in refs}
    callee_names = {r.callee_name for r in refs}
    assert "App.Foo.Alpha" in caller_names
    assert "App.Foo.Beta" in callee_names
