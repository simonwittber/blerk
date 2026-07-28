from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import tree_sitter_c
import tree_sitter_c_sharp
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
from tree_sitter import Language, Parser, Query

from blerk.symbols import queries, regexp_extractor
from blerk.symbols.types import SNIPPET_MAX_LINES, CallRef, Symbol


try:
    from tree_sitter import QueryCursor as _QueryCursor
except ImportError:
    _QueryCursor = None


GO_LANG = Language(tree_sitter_go.language())
PY_LANG = Language(tree_sitter_python.language())
JS_LANG = Language(tree_sitter_javascript.language())
C_LANG = Language(tree_sitter_c.language())
CPP_LANG = Language(tree_sitter_cpp.language())
CS_LANG = Language(tree_sitter_c_sharp.language())


@dataclass
class LangDef:
    key: str
    language: Language
    decl_query: str
    call_query: str
    body_types: list[str]


LANG_DEFS: list[LangDef] = [
    LangDef("go", GO_LANG, queries.GO_DECL, queries.GO_CALL, queries.BODY_TYPES["go"]),
    LangDef("py", PY_LANG, queries.PY_DECL, queries.PY_CALL, queries.BODY_TYPES["py"]),
    LangDef("js", JS_LANG, queries.JS_DECL, queries.JS_CALL, queries.BODY_TYPES["js"]),
    LangDef("c", C_LANG, queries.C_DECL, queries.C_CALL, queries.BODY_TYPES["c"]),
    LangDef("cpp", CPP_LANG, queries.CPP_DECL, queries.CPP_CALL, queries.BODY_TYPES["cpp"]),
    LangDef("cs", CS_LANG, queries.CS_DECL, queries.CS_CALL, queries.BODY_TYPES["cs"]),
]


_EXT_TO_LANG: dict[str, LangDef] = {}
for _d in LANG_DEFS:
    if _d.key == "go":
        _EXT_TO_LANG[".go"] = _d
    elif _d.key == "py":
        _EXT_TO_LANG[".py"] = _d
    elif _d.key == "js":
        _EXT_TO_LANG[".js"] = _d
        _EXT_TO_LANG[".jsx"] = _d
        _EXT_TO_LANG[".ts"] = _d
        _EXT_TO_LANG[".tsx"] = _d
    elif _d.key == "c":
        _EXT_TO_LANG[".c"] = _d
        _EXT_TO_LANG[".h"] = _d
    elif _d.key == "cpp":
        _EXT_TO_LANG[".cpp"] = _d
        _EXT_TO_LANG[".cc"] = _d
        _EXT_TO_LANG[".cxx"] = _d
        _EXT_TO_LANG[".hpp"] = _d
    elif _d.key == "cs":
        _EXT_TO_LANG[".cs"] = _d


def _run_matches(query: Query, node: Any) -> list[tuple[int, dict[str, list[Any]]]]:
    if _QueryCursor is not None:
        return _QueryCursor(query).matches(node)
    return query.matches(node)


class Extractor:
    def extract(self, path: str) -> tuple[list[Symbol], list[CallRef]]:
        ext = os.path.splitext(path)[1].lower()
        ld = _EXT_TO_LANG.get(ext)
        if ld is None:
            syms = regexp_extractor.extract_symbols(path)
            return syms, []
        try:
            with open(path, "rb") as f:
                src = f.read()
        except OSError:
            return [], []
        parser = Parser(ld.language)
        tree = parser.parse(src)
        if tree is None:
            return [], []
        root = tree.root_node
        syms = extract_decls(root, src, ld)
        refs = extract_calls(root, src, ld, syms)
        return syms, refs


def _node_text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def extract_decls(root: Any, src: bytes, ld: LangDef) -> list[Symbol]:
    try:
        q = Query(ld.language, ld.decl_query)
    except Exception:
        return []
    matches = _run_matches(q, root)
    out: list[Symbol] = []
    for _pattern_idx, captures in matches:
        name_node = None
        def_node = None
        kind = ""
        for cap_name, nodes in captures.items():
            if not nodes:
                continue
            node = nodes[0]
            if cap_name == "name":
                name_node = node
            elif cap_name == "func":
                def_node = node
                kind = "function"
            elif cap_name == "method":
                def_node = node
                kind = "method"
            elif cap_name == "class":
                def_node = node
                kind = "class"
            elif cap_name == "interface":
                def_node = node
                kind = "interface"
            elif cap_name == "struct":
                def_node = node
                kind = "struct"
            elif cap_name == "enum":
                def_node = node
                kind = "enum"
            elif cap_name == "type":
                def_node = node
                kind = "type"
        if name_node is None or def_node is None:
            continue
        name = _node_text(name_node, src)
        if ld.key == "py" and kind == "function" and is_inside_class(def_node):
            kind = "method"
        out.append(Symbol(
            name=name,
            kind=kind,
            line=def_node.start_point[0] + 1,
            end_line=def_node.end_point[0] + 1,
            snippet=snippet_content(def_node, src),
        ))
    return out


def extract_calls(root: Any, src: bytes, ld: LangDef, syms: list[Symbol]) -> list[CallRef]:
    try:
        q = Query(ld.language, ld.call_query)
    except Exception:
        return []
    body_types = ld.body_types
    refs: list[CallRef] = []
    for sym in syms:
        if sym.kind != "function" and sym.kind != "method":
            continue
        def_node = node_at_line(root, sym.line - 1)
        if def_node is None:
            continue
        body = find_body_child(def_node, body_types)
        if body is None:
            continue
        matches = _run_matches(q, body)
        for _pattern_idx, captures in matches:
            callees = captures.get("callee") or []
            for callee_node in callees:
                callee = _node_text(callee_node, src)
                if callee != sym.name:
                    refs.append(CallRef(caller_name=sym.name, callee_name=callee))
    return refs


def node_at_line(root: Any, row: int) -> Any:
    node = root
    while True:
        found = False
        for i in range(node.child_count):
            child = node.child(i)
            if child.start_point[0] == row:
                node = child
                found = True
                break
            if child.start_point[0] < row and child.end_point[0] > row:
                node = child
                found = True
                break
        if not found:
            break
    if node.start_point[0] == row:
        return node
    return None


def find_body_child(n: Any, body_types: list[str]) -> Any:
    for i in range(n.child_count):
        child = n.child(i)
        if child.type in body_types:
            return child
    parent = n.parent
    if parent is not None:
        for i in range(parent.child_count):
            child = parent.child(i)
            if child.type in body_types:
                return child
    return None


def is_inside_class(n: Any) -> bool:
    p = n.parent
    if p is None:
        return False
    gp = p.parent
    return gp is not None and gp.type == "class_definition"


def snippet_content(node: Any, src: bytes) -> str:
    content = _node_text(node, src)
    lines = content.split("\n", SNIPPET_MAX_LINES)
    if len(lines) > SNIPPET_MAX_LINES:
        lines = lines[:SNIPPET_MAX_LINES]
    return "\n".join(lines)
