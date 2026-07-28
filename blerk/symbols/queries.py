GO_DECL = """
(function_declaration name: (identifier) @name) @func
(method_declaration name: (field_identifier) @name) @method
(type_declaration (type_spec name: (type_identifier) @name)) @type
"""

GO_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (selector_expression field: (field_identifier) @callee))
"""

PY_DECL = """
(function_definition name: (identifier) @name) @func
(class_definition name: (identifier) @name) @class
"""

PY_CALL = """
(call function: (identifier) @callee)
(call function: (attribute attribute: (identifier) @callee))
"""

JS_DECL = """
(function_declaration name: (identifier) @name) @func
(class_declaration name: (identifier) @name) @class
(method_definition name: (property_identifier) @name) @method
"""

JS_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (member_expression property: (property_identifier) @callee))
"""

C_DECL = """
(function_definition declarator: (function_declarator declarator: (identifier) @name)) @func
(struct_specifier name: (type_identifier) @name body: (_)) @struct
"""

C_CALL = """
(call_expression function: (identifier) @callee)
"""

CPP_DECL = """
(function_definition declarator: (function_declarator declarator: (identifier) @name)) @func
(function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name))) @func
(class_specifier name: (type_identifier) @name body: (_)) @class
(struct_specifier name: (type_identifier) @name body: (_)) @struct
"""

CPP_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (field_expression field: (field_identifier) @callee))
(call_expression function: (qualified_identifier name: (identifier) @callee))
"""

CS_DECL = """
(method_declaration name: (identifier) @name) @method
(class_declaration name: (identifier) @name) @class
(interface_declaration name: (identifier) @name) @interface
(struct_declaration name: (identifier) @name) @struct
(enum_declaration name: (identifier) @name) @enum
(constructor_declaration name: (identifier) @name) @method
"""

CS_CALL = """
(invocation_expression function: (identifier) @callee)
(invocation_expression function: (member_access_expression name: (identifier) @callee))
"""

BODY_TYPES: dict[str, list[str]] = {
    "go": ["block"],
    "py": ["block"],
    "js": ["statement_block"],
    "c": ["compound_statement"],
    "cpp": ["compound_statement"],
    "cs": ["block"],
}
