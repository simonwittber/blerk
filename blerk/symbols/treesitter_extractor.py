from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Any

import tree_sitter_c
import tree_sitter_c_sharp
import tree_sitter_cpp
import tree_sitter_glsl
import tree_sitter_go
import tree_sitter_hlsl
import tree_sitter_javascript
import tree_sitter_python
from tree_sitter import Language, Parser, Query

from blerk.symbols import queries
from blerk.symbols.types import CallRef, Symbol, count_params


try:
    from tree_sitter import QueryCursor as _QueryCursor
except ImportError:
    _QueryCursor = None


def _legacy_language(mod) -> Language:
    # tree-sitter-hlsl and tree-sitter-glsl return a raw int pointer (old 0.22
    # ABI) rather than a PyCapsule. Wrap the pointer so tree-sitter 0.23+ accepts it.
    ptr = mod.language()
    api = ctypes.pythonapi
    api.PyCapsule_New.restype = ctypes.py_object
    api.PyCapsule_New.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return Language(api.PyCapsule_New(ptr, b"tree_sitter.Language", None))


GO_LANG = Language(tree_sitter_go.language())
PY_LANG = Language(tree_sitter_python.language())
JS_LANG = Language(tree_sitter_javascript.language())
C_LANG = Language(tree_sitter_c.language())
CPP_LANG = Language(tree_sitter_cpp.language())
CS_LANG = Language(tree_sitter_c_sharp.language())
HLSL_LANG = _legacy_language(tree_sitter_hlsl)
GLSL_LANG = _legacy_language(tree_sitter_glsl)


@dataclass
class LangDef:
    key: str
    language: Language
    decl_query: str
    call_query: str
    body_types: list[str]


LANG_DEFS: list[LangDef] = [
    LangDef("go",   GO_LANG,   queries.GO_DECL,   queries.GO_CALL,   queries.BODY_TYPES["go"]),
    LangDef("py",   PY_LANG,   queries.PY_DECL,   queries.PY_CALL,   queries.BODY_TYPES["py"]),
    LangDef("js",   JS_LANG,   queries.JS_DECL,   queries.JS_CALL,   queries.BODY_TYPES["js"]),
    LangDef("c",    C_LANG,    queries.C_DECL,    queries.C_CALL,    queries.BODY_TYPES["c"]),
    LangDef("cpp",  CPP_LANG,  queries.CPP_DECL,  queries.CPP_CALL,  queries.BODY_TYPES["cpp"]),
    LangDef("cs",   CS_LANG,   queries.CS_DECL,   queries.CS_CALL,   queries.BODY_TYPES["cs"]),
    LangDef("hlsl", HLSL_LANG, queries.HLSL_DECL, queries.HLSL_CALL, queries.BODY_TYPES["hlsl"]),
    LangDef("glsl", GLSL_LANG, queries.GLSL_DECL, queries.GLSL_CALL, queries.BODY_TYPES["glsl"]),
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
    elif _d.key == "hlsl":
        _EXT_TO_LANG[".hlsl"] = _d
        _EXT_TO_LANG[".fx"] = _d
        _EXT_TO_LANG[".fxh"] = _d
        _EXT_TO_LANG[".hlsli"] = _d
    elif _d.key == "glsl":
        _EXT_TO_LANG[".glsl"] = _d
        _EXT_TO_LANG[".vert"] = _d
        _EXT_TO_LANG[".frag"] = _d
        _EXT_TO_LANG[".geom"] = _d
        _EXT_TO_LANG[".comp"] = _d
        _EXT_TO_LANG[".tese"] = _d
        _EXT_TO_LANG[".tesc"] = _d


_CONTAINER_TYPES = frozenset({
    "function_definition",
    "class_definition",
    "function_declaration",
    "method_declaration",
    "class_declaration",
    "method_definition",
    "class_specifier",
    "struct_specifier",
    "struct_declaration",
    "interface_declaration",
    "namespace_declaration",
    "constructor_declaration",
})

_CLASS_NODE_TYPES: dict[str, frozenset[str]] = {
    "cs":   frozenset({"class_declaration", "struct_declaration", "interface_declaration"}),
    "py":   frozenset({"class_definition"}),
    "js":   frozenset({"class_declaration"}),
    "cpp":  frozenset({"class_specifier", "struct_specifier"}),
    "c":    frozenset({"struct_specifier"}),
    "go":   frozenset(),
    "hlsl": frozenset({"struct_specifier"}),
    "glsl": frozenset({"struct_specifier"}),
}


def _nesting_depth(node: Any) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        if parent.type in _CONTAINER_TYPES:
            depth += 1
        parent = parent.parent
    return depth


def _enclosing_class_names(node: Any, src: bytes, lang_key: str) -> list[str]:
    container = _CLASS_NODE_TYPES.get(lang_key, frozenset())
    parts: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type in container:
            name_node = parent.child_by_field_name("name")
            if name_node:
                parts.append(_node_text(name_node, src))
        parent = parent.parent
    parts.reverse()
    return parts


def _go_receiver_type(def_node: Any, src: bytes) -> str:
    if def_node.type != "method_declaration":
        return ""
    receiver = def_node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    for i in range(receiver.child_count):
        child = receiver.child(i)
        if child.type == "parameter_declaration":
            for j in range(child.child_count):
                gc = child.child(j)
                if gc.type == "type_identifier":
                    return _node_text(gc, src)
                if gc.type == "pointer_type":
                    for k in range(gc.child_count):
                        if gc.child(k).type == "type_identifier":
                            return _node_text(gc.child(k), src)
    return ""


def _build_qualified_name(short_name: str, def_node: Any, src: bytes, lang_key: str, namespace: str) -> str:
    parts: list[str] = []
    if namespace:
        parts.append(namespace)
    if lang_key == "go":
        receiver = _go_receiver_type(def_node, src)
        if receiver:
            parts.append(receiver)
    else:
        parts.extend(_enclosing_class_names(def_node, src, lang_key))
    parts.append(short_name)
    return ".".join(parts)


def _run_matches(query: Query, node: Any) -> list[tuple[int, dict[str, list[Any]]]]:
    if _QueryCursor is not None:
        return _QueryCursor(query).matches(node)
    return query.matches(node)


class Extractor:
    def extract(self, path: str) -> tuple[list[Symbol], list[CallRef]]:
        if os.path.basename(path) == "package.json":
            from blerk.symbols import package_json_extractor
            return package_json_extractor.extract(path), []
        ext = os.path.splitext(path)[1].lower()
        if ext in (".yaml", ".yml"):
            from blerk.symbols import yaml_extractor
            return yaml_extractor.extract(path), []
        ld = _EXT_TO_LANG.get(ext)
        if ld is None:
            return [], []
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
        syms = extract_decls(root, src, ld, path)
        refs = extract_calls(root, src, ld, syms)
        return syms, refs


def _node_text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def extract_decls(root: Any, src: bytes, ld: LangDef, path: str = "") -> list[Symbol]:
    try:
        q = Query(ld.language, ld.decl_query)
    except Exception:
        return []
    matches = _run_matches(q, root)
    out: list[Symbol] = []
    for _pattern_idx, captures in matches:
        name_node = None
        def_node = None
        params_node = None
        kind = ""
        for cap_name, nodes in captures.items():
            if not nodes:
                continue
            node = nodes[0]
            if cap_name == "name":
                name_node = node
            elif cap_name == "params":
                params_node = node
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
            elif cap_name == "field":
                def_node = node
                kind = "field"
            elif cap_name == "variable":
                def_node = node
                kind = "variable"
        if name_node is None or def_node is None:
            continue
        short_name = _node_text(name_node, src)
        if ld.key == "py" and kind == "function" and is_inside_class(def_node):
            kind = "method"
        params = ""
        if params_node is not None:
            raw = _node_text(params_node, src).strip()
            inner = raw[1:-1] if raw.startswith("(") else raw
            params = " ".join(inner.split())
        tags = _symbol_tags(def_node, src, ld.key, short_name, root=root, path=path)
        namespace = tags.get("namespace", "")
        qualified = _build_qualified_name(short_name, def_node, src, ld.key, namespace)
        out.append(Symbol(
            name=qualified,
            short_name=short_name,
            kind=kind,
            line=def_node.start_point[0] + 1,
            end_line=def_node.end_point[0] + 1,
            snippet=snippet_content(def_node, src),
            params=params,
            nesting_depth=_nesting_depth(def_node),
            param_count=count_params(params),
            tags=tags,
        ))
    return out


_MEMBER_ACCESS_PARENT: dict[str, str] = {
    "cs":   "member_access_expression",
    "py":   "attribute",
    "go":   "selector_expression",
    "js":   "member_expression",
    "cpp":  "field_expression",
}

_MEMBER_ACCESS_OBJ_FIELD: dict[str, str] = {
    "cs":  "expression",
    "py":  "object",
    "go":  "operand",
    "js":  "object",
    "cpp": "argument",
}

_GO_RECV_DECL = """
(method_declaration
  receiver: (parameter_list
    (parameter_declaration
      name: (identifier) @recv_var
      type: (type_identifier) @recv_type))
  name: (field_identifier) @method_name)
(method_declaration
  receiver: (parameter_list
    (parameter_declaration
      name: (identifier) @recv_var
      type: (pointer_type (type_identifier) @recv_type)))
  name: (field_identifier) @method_name)
"""

_CS_TYPE_DECL = """
(field_declaration (variable_declaration type: (identifier) @decl_type (variable_declarator name: (identifier) @decl_name)))
(parameter type: (identifier) @decl_type name: (identifier) @decl_name)
"""


def _build_field_type_map(root: Any, src: bytes, lang_key: str) -> dict[tuple[str, str], str]:
    """Return {(immediate_class_name, variable_name): declared_type} for C# only."""
    if lang_key != "cs":
        return {}
    try:
        q = Query(CS_LANG, _CS_TYPE_DECL)
    except Exception:
        return {}
    result: dict[tuple[str, str], str] = {}
    for _, captures in _run_matches(q, root):
        type_node = (captures.get("decl_type") or [None])[0]
        name_node = (captures.get("decl_name") or [None])[0]
        if type_node is None or name_node is None:
            continue
        type_name = _node_text(type_node, src)
        var_name = _node_text(name_node, src)
        class_names = _enclosing_class_names(name_node, src, lang_key)
        class_key = class_names[-1] if class_names else ""
        result[(class_key, var_name)] = type_name
    return result


def _build_go_recv_map(
    root: Any, src: bytes, syms: list[Symbol],
) -> dict[str, tuple[str, str]]:
    """Return {qualified_method_name: (recv_var, qualified_recv_prefix)} for Go methods."""
    try:
        q = Query(GO_LANG, _GO_RECV_DECL)
    except Exception:
        return {}

    type_short_to_prefix: dict[str, str] = {}
    for sym in syms:
        if sym.kind in ("function", "method") and "." in sym.name:
            prefix = sym.name.rsplit(".", 1)[0]
            type_short_to_prefix[prefix.split(".")[-1]] = prefix

    method_short_to_qualified: dict[str, list[str]] = {}
    for sym in syms:
        if sym.kind in ("function", "method"):
            short = sym.short_name or sym.name.split(".")[-1]
            method_short_to_qualified.setdefault(short, []).append(sym.name)

    result: dict[str, tuple[str, str]] = {}
    for _, captures in _run_matches(q, root):
        rv_node = (captures.get("recv_var") or [None])[0]
        rt_node = (captures.get("recv_type") or [None])[0]
        mn_node = (captures.get("method_name") or [None])[0]
        if rv_node is None or rt_node is None or mn_node is None:
            continue
        recv_var = _node_text(rv_node, src)
        recv_type_short = _node_text(rt_node, src)
        method_short = _node_text(mn_node, src)
        qualified_recv = type_short_to_prefix.get(recv_type_short, recv_type_short)
        qualified_method: str | None = None
        for qname in method_short_to_qualified.get(method_short, []):
            prefix = qname.rsplit(".", 1)[0]
            if prefix.split(".")[-1] == recv_type_short:
                qualified_method = qname
                break
        if qualified_method:
            result[qualified_method] = (recv_var, qualified_recv)

    return result


def _qualify_callee(
    callee_node: Any,
    callee_short: str,
    caller_name: str,
    class_method_to_qualified: dict[tuple[str, str], str],
    short_to_qualified: dict[str, str],
    field_type_map: dict[tuple[str, str], str],
    lang_key: str,
    src: bytes,
    go_recv_map: dict[str, tuple[str, str]] | None = None,
) -> str:
    caller_class = caller_name.rsplit(".", 1)[0] if "." in caller_name else ""
    parent = callee_node.parent
    member_access_type = _MEMBER_ACCESS_PARENT.get(lang_key)
    is_member_access = parent is not None and member_access_type and parent.type == member_access_type

    if is_member_access:
        obj_field = _MEMBER_ACCESS_OBJ_FIELD.get(lang_key, "expression")
        expr_node = parent.child_by_field_name(obj_field)
        if expr_node is not None:
            obj_name = _node_text(expr_node, src)
            if field_type_map:
                caller_class_short = caller_class.split(".")[-1] if caller_class else ""
                declared_type = field_type_map.get((caller_class_short, obj_name))
                if declared_type:
                    return f"{declared_type}.{callee_short}"
            if go_recv_map is not None and lang_key == "go":
                recv_info = go_recv_map.get(caller_name)
                if recv_info is not None:
                    recv_var, qualified_recv = recv_info
                    if obj_name == recv_var:
                        qualified = class_method_to_qualified.get((qualified_recv, callee_short))
                        if qualified:
                            return qualified

    if not is_member_access and caller_class:
        qualified = class_method_to_qualified.get((caller_class, callee_short))
        if qualified:
            return qualified

    return short_to_qualified.get(callee_short, callee_short)


def extract_calls(root: Any, src: bytes, ld: LangDef, syms: list[Symbol]) -> list[CallRef]:
    try:
        q = Query(ld.language, ld.call_query)
    except Exception:
        return []
    ranges = sorted(
        (sym.line, sym.end_line, sym.name)
        for sym in syms
        if sym.kind in ("function", "method")
    )
    if not ranges:
        return []

    # (class_prefix, short_name) → qualified: for bare same-class calls
    class_method_to_qualified: dict[tuple[str, str], str] = {}
    for sym in syms:
        if sym.kind in ("function", "method") and "." in sym.name:
            prefix = sym.name.rsplit(".", 1)[0]
            short = sym.short_name or sym.name.split(".")[-1]
            class_method_to_qualified[(prefix, short)] = sym.name

    # flat short_name → qualified: only for names that are unique across the file
    short_counts: dict[str, int] = {}
    for sym in syms:
        if sym.kind in ("function", "method"):
            k = sym.short_name or sym.name.split(".")[-1]
            short_counts[k] = short_counts.get(k, 0) + 1
    short_to_qualified = {
        (sym.short_name or sym.name.split(".")[-1]): sym.name
        for sym in syms
        if sym.kind in ("function", "method")
        and short_counts[sym.short_name or sym.name.split(".")[-1]] == 1
    }

    field_type_map = _build_field_type_map(root, src, ld.key)
    go_recv_map: dict[str, tuple[str, str]] | None = None
    if ld.key == "go":
        go_recv_map = _build_go_recv_map(root, src, syms)

    seen: set[tuple[str, str]] = set()
    refs: list[CallRef] = []
    for _pattern_idx, captures in _run_matches(q, root):
        for callee_node in (captures.get("callee") or []):
            callee_line = callee_node.start_point[0] + 1
            callee_short = _node_text(callee_node, src)
            caller_name = None
            for start, end, name in ranges:
                if start <= callee_line <= end:
                    caller_name = name
                    break
            if caller_name is None:
                continue
            callee = _qualify_callee(
                callee_node, callee_short, caller_name,
                class_method_to_qualified, short_to_qualified,
                field_type_map, ld.key, src,
                go_recv_map=go_recv_map,
            )
            key = (caller_name, callee)
            if callee != caller_name and key not in seen:
                seen.add(key)
                refs.append(CallRef(caller_name=caller_name, callee_name=callee))
    return refs


_VISIBILITY_KEYWORDS = frozenset({"public", "private", "protected", "internal"})


def _cs_namespace(node: Any, src: bytes) -> str:
    parts: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
            name_node = parent.child_by_field_name("name")
            if name_node:
                parts.append(_node_text(name_node, src))
        parent = parent.parent
    parts.reverse()
    return ".".join(parts)


def _go_package(root: Any, src: bytes) -> str:
    for child in root.children:
        if child.type == "package_clause":
            for c in child.children:
                if c.type == "package_identifier":
                    return _node_text(c, src)
    return ""


def _py_module_name(path: str) -> str:
    from pathlib import Path as _Path
    p = _Path(path)
    parts = [p.stem]
    current = p.parent
    while (current / "__init__.py").exists():
        parts.append(current.name)
        current = current.parent
    parts.reverse()
    return ".".join(parts)


def _cpp_namespace(node: Any, src: bytes) -> str:
    parts: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type == "namespace_definition":
            name_node = parent.child_by_field_name("name")
            if name_node:
                parts.append(_node_text(name_node, src))
        parent = parent.parent
    parts.reverse()
    return ".".join(parts)


def _extract_namespace(node: Any, src: bytes, lang_key: str, root: Any, path: str) -> str:
    if lang_key == "cs":
        return _cs_namespace(node, src)
    if lang_key == "go":
        return _go_package(root, src) if root is not None else ""
    if lang_key == "py":
        return _py_module_name(path) if path else ""
    if lang_key == "cpp":
        return _cpp_namespace(node, src)
    return ""


def _symbol_tags(node: Any, src: bytes, lang_key: str, name: str, root: Any = None, path: str = "") -> dict[str, str]:
    tags: dict[str, str] = {}
    if lang_key in ("cs", "go", "c", "cpp", "js"):
        modifiers: list[str] = []
        for i in range(node.child_count):
            child = node.child(i)
            if child.type == "modifier":
                modifiers.append(_node_text(child, src).strip())
        if "static" in modifiers:
            tags["is_static"] = "true"
        vis_mods = [m for m in modifiers if m in _VISIBILITY_KEYWORDS]
        if vis_mods:
            tags["visibility"] = " ".join(sorted(vis_mods, key=lambda m: modifiers.index(m)))
    if lang_key == "go":
        if name and name[0].isupper():
            tags["visibility"] = "public"
        elif name:
            tags["visibility"] = "private"
    if lang_key == "py":
        if name.startswith("__") and not name.endswith("__"):
            tags["visibility"] = "private"
        elif name.startswith("_"):
            tags["visibility"] = "private"
        else:
            tags["visibility"] = "public"
    ns = _extract_namespace(node, src, lang_key, root, path)
    if ns:
        tags["namespace"] = ns
    return tags


def is_inside_class(n: Any) -> bool:
    p = n.parent
    if p is None:
        return False
    gp = p.parent
    return gp is not None and gp.type == "class_definition"


def snippet_content(node: Any, src: bytes) -> str:
    return _node_text(node, src)
