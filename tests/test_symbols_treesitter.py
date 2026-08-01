from __future__ import annotations

from blerk.symbols.treesitter_extractor import Extractor


def symbol_names(syms):
    return [s.name for s in syms]


def test_go_functions(write_temp):
    src = "package main\n\nfunc Foo(x int) int { return x }\n\nfunc Bar() {}\n"
    path = write_temp("a.go", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "main.Foo" in names and "main.Bar" in names
    for s in syms:
        assert s.kind in ("function", "method", "type")


def test_go_no_keyword_symbols(write_temp):
    src = (
        "package main\n\nfunc Run() {\n\tfor i := 0; i < 10; i++ {\n"
        "\t\tif i > 5 {\n\t\t\treturn\n\t\t}\n\t}\n}\n"
    )
    path = write_temp("b.go", src)
    syms, _ = Extractor().extract(path)
    for s in syms:
        assert s.name not in ("for", "if", "return", "range")


def test_go_call_refs(write_temp):
    src = "package main\n\nfunc Alpha() {\n\tBeta()\n}\n\nfunc Beta() {}\n"
    path = write_temp("c.go", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "main.Alpha" and r.callee_name == "main.Beta" for r in refs)


def test_python_class_and_method(write_temp):
    src = "class MyClass:\n    def my_method(self):\n        pass\n\ndef top_level():\n    pass\n"
    path = write_temp("d.py", src)
    syms, _ = Extractor().extract(path)
    kinds = {s.name: s.kind for s in syms}
    assert any(k.endswith("MyClass") and v == "class" for k, v in kinds.items())
    assert any(k.endswith("MyClass.my_method") and v == "method" for k, v in kinds.items())
    assert any(k.endswith("top_level") and v == "function" for k, v in kinds.items())


def test_js_function_and_class(write_temp):
    src = "function greet(name) { return name; }\n\nclass Animal {\n  speak() { return \"\"; }\n}\n"
    path = write_temp("e.js", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "greet" in names
    assert "Animal" in names


def test_cs_class_and_method(write_temp):
    src = "public class Greeter {\n    public void Hello() {}\n}\n"
    path = write_temp("f.cs", src)
    syms, _ = Extractor().extract(path)
    kinds = {s.name: s.kind for s in syms}
    assert kinds.get("Greeter") == "class"
    assert kinds.get("Greeter.Hello") == "method"


def test_markdown_fallback(write_temp):
    src = "# Heading\n\nSome text.\n"
    path = write_temp("g.md", src)
    syms, refs = Extractor().extract(path)
    assert refs == []
    assert any(s.kind == "heading" and s.name == "Heading" for s in syms)


def test_line_numbers(write_temp):
    src = "package main\n\nfunc First() {}\n\nfunc Second() {}\n"
    path = write_temp("h.go", src)
    syms, _ = Extractor().extract(path)
    line_of = {s.name: s.line for s in syms}
    assert line_of.get("main.First") == 3
    assert line_of.get("main.Second") == 5


def test_snippet_cap(write_temp):
    body = "\n".join([f"    x{i} = {i}" for i in range(200)])
    src = f"def big():\n{body}\n"
    path = write_temp("big.py", src)
    syms, _ = Extractor().extract(path)
    big = next(s for s in syms if s.short_name == "big")
    assert big.snippet.count("\n") + 1 <= 100


def test_unsupported_ext_delegates(write_temp):
    src = "# heading\ncontent\n"
    path = write_temp("x.md", src)
    syms, refs = Extractor().extract(path)
    assert refs == []
    assert any(s.name == "heading" for s in syms)


# ---------------------------------------------------------------------------
# Qualified name tests
# These assert that symbol names include namespace/class context.
# ---------------------------------------------------------------------------

def test_cs_qualified_name_namespace_and_class(write_temp):
    src = (
        "namespace MyApp.Core {\n"
        "    public class Processor {\n"
        "        public void Execute() {}\n"
        "    }\n"
        "}\n"
    )
    path = write_temp("q.cs", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "MyApp.Core.Processor.Execute" in names


def test_cs_qualified_name_class_only(write_temp):
    src = (
        "public class Greeter {\n"
        "    public void Hello() {}\n"
        "}\n"
    )
    path = write_temp("r.cs", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "Greeter.Hello" in names


def test_py_qualified_name_class_and_method(write_temp):
    src = (
        "class MyService:\n"
        "    def process(self):\n"
        "        pass\n"
    )
    path = write_temp("s.py", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert any("MyService.process" in n for n in names)


def test_go_qualified_name_package_and_type(write_temp):
    src = (
        "package core\n\n"
        "type Worker struct{}\n\n"
        "func (w Worker) Run() {}\n"
    )
    path = write_temp("t.go", src)
    syms, _ = Extractor().extract(path)
    names = symbol_names(syms)
    assert "core.Worker.Run" in names


def test_call_refs_use_short_names(write_temp):
    src = (
        "namespace App {\n"
        "    public class Foo {\n"
        "        public void Alpha() { Beta(); }\n"
        "        public void Beta() {}\n"
        "    }\n"
        "}\n"
    )
    path = write_temp("u.cs", src)
    _, refs = Extractor().extract(path)
    caller_names = {r.caller_name for r in refs}
    callee_names = {r.callee_name for r in refs}
    assert "App.Foo.Alpha" in caller_names
    assert "App.Foo.Beta" in callee_names
