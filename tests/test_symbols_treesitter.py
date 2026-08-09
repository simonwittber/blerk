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



def test_line_numbers(write_temp):
    src = "package main\n\nfunc First() {}\n\nfunc Second() {}\n"
    path = write_temp("h.go", src)
    syms, _ = Extractor().extract(path)
    line_of = {s.name: s.line for s in syms}
    assert line_of.get("main.First") == 3
    assert line_of.get("main.Second") == 5


def test_snippet_full_content(write_temp):
    body = "\n".join([f"    x{i} = {i}" for i in range(200)])
    src = f"def big():\n{body}\n"
    path = write_temp("big.py", src)
    syms, _ = Extractor().extract(path)
    big = next(s for s in syms if s.short_name == "big")
    assert big.snippet.count("\n") + 1 == 201


def test_unsupported_ext_returns_empty(write_temp):
    src = "# heading\ncontent\n"
    path = write_temp("x.md", src)
    syms, refs = Extractor().extract(path)
    assert syms == []
    assert refs == []


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


def test_go_receiver_call_resolves_to_own_type(write_temp):
    """s.Process() inside Service.Run must resolve to svc.Service.Process."""
    src = (
        "package svc\n\n"
        "type Service struct{}\n\n"
        "func (s *Service) Run() {\n"
        "    s.Process()\n"
        "}\n\n"
        "func (s *Service) Process() {}\n"
    )
    path = write_temp("recv.go", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "svc.Service.Run" and r.callee_name == "svc.Service.Process" for r in refs), \
        f"Expected svc.Service.Run->svc.Service.Process, got: {[(r.caller_name, r.callee_name) for r in refs]}"


def test_go_receiver_cross_type_no_collision(write_temp):
    """Two types with same method name: each receiver call must resolve to its own type."""
    src = (
        "package app\n\n"
        "type Alpha struct{}\n"
        "type Beta struct{}\n\n"
        "func (a *Alpha) Run() { a.Work() }\n"
        "func (b *Beta) Run() { b.Work() }\n\n"
        "func (a *Alpha) Work() {}\n"
        "func (b *Beta) Work() {}\n"
    )
    path = write_temp("cross.go", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "app.Alpha.Run" and r.callee_name == "app.Alpha.Work" for r in refs), \
        f"Expected app.Alpha.Run->app.Alpha.Work, got: {[(r.caller_name, r.callee_name) for r in refs]}"
    assert any(r.caller_name == "app.Beta.Run" and r.callee_name == "app.Beta.Work" for r in refs), \
        f"Expected app.Beta.Run->app.Beta.Work, got: {[(r.caller_name, r.callee_name) for r in refs]}"


# ---------------------------------------------------------------------------
# Call resolution: class-aware and declared-type qualification
# ---------------------------------------------------------------------------

def test_cs_bare_call_resolves_to_own_class(write_temp):
    """
    When two classes in the same file share a method name, a bare call inside
    ClassA must resolve to ClassA's method, not ClassB's.
    Currently FAILS: short_to_qualified is a flat dict — last writer wins.
    """
    src = """\
public class ClassA {
    public void Update() { }
    public void Run() { Update(); }
}
public class ClassB {
    public void Update() { }
    public void Execute() { Update(); }
}
"""
    path = write_temp("collision.cs", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "ClassA.Run" and r.callee_name == "ClassA.Update" for r in refs), \
        f"Expected ClassA.Run->ClassA.Update, got: {[(r.caller_name, r.callee_name) for r in refs]}"
    assert any(r.caller_name == "ClassB.Execute" and r.callee_name == "ClassB.Update" for r in refs), \
        f"Expected ClassB.Execute->ClassB.Update, got: {[(r.caller_name, r.callee_name) for r in refs]}"


def test_cs_member_access_uses_declared_field_type(write_temp):
    """
    When two classes have fields of different types but both named _engine,
    member access calls must be qualified using each class's declared field type.
    Currently FAILS: short_to_qualified['Execute'] is one of EngineA or EngineB arbitrarily.
    """
    src = """\
public class ClassA {
    private EngineA _engine;
    public void Run() { _engine.Execute(); }
}
public class ClassB {
    private EngineB _engine;
    public void Run() { _engine.Execute(); }
}
public class EngineA {
    public void Execute() { }
}
public class EngineB {
    public void Execute() { }
}
"""
    path = write_temp("field_types.cs", src)
    _, refs = Extractor().extract(path)
    assert any(r.caller_name == "ClassA.Run" and r.callee_name == "EngineA.Execute" for r in refs), \
        f"Expected ClassA.Run->EngineA.Execute, got: {[(r.caller_name, r.callee_name) for r in refs]}"
    assert any(r.caller_name == "ClassB.Run" and r.callee_name == "EngineB.Execute" for r in refs), \
        f"Expected ClassB.Run->EngineB.Execute, got: {[(r.caller_name, r.callee_name) for r in refs]}"


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
