GO_DECL = """
(function_declaration name: (identifier) @name parameters: (parameter_list) @params) @func
(method_declaration name: (field_identifier) @name parameters: (parameter_list) @params) @method
(type_declaration (type_spec name: (type_identifier) @name)) @type
(source_file (var_declaration (var_spec name: (identifier) @name) @variable))
(source_file (const_declaration (const_spec name: (identifier) @name) @variable))
"""

GO_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (selector_expression field: (field_identifier) @callee))
"""

PY_DECL = """
(function_definition name: (identifier) @name parameters: (parameters) @params) @func
(class_definition name: (identifier) @name) @class
(module (expression_statement (assignment left: (identifier) @name) @variable))
(class_definition body: (block (expression_statement (assignment left: (identifier) @name) @variable)))
"""

PY_CALL = """
(call function: (identifier) @callee)
(call function: (attribute attribute: (identifier) @callee))
"""

JS_DECL = """
(function_declaration name: (identifier) @name parameters: (formal_parameters) @params) @func
(class_declaration name: (identifier) @name) @class
(method_definition name: (property_identifier) @name parameters: (formal_parameters) @params) @method
(program (lexical_declaration (variable_declarator name: (identifier) @name) @variable))
(program (variable_declaration (variable_declarator name: (identifier) @name) @variable))
"""

JS_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (member_expression property: (property_identifier) @callee))
"""

C_DECL = """
(function_definition declarator: (function_declarator declarator: (identifier) @name parameters: (parameter_list) @params)) @func
(struct_specifier name: (type_identifier) @name body: (_)) @struct
(translation_unit (declaration declarator: (init_declarator declarator: (identifier) @name) @variable))
"""

C_CALL = """
(call_expression function: (identifier) @callee)
"""

CPP_DECL = """
(function_definition declarator: (function_declarator declarator: (identifier) @name parameters: (parameter_list) @params)) @func
(function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name) parameters: (parameter_list) @params)) @func
(class_specifier name: (type_identifier) @name body: (_)) @class
(struct_specifier name: (type_identifier) @name body: (_)) @struct
(translation_unit (declaration declarator: (init_declarator declarator: (identifier) @name) @variable))
"""

CPP_CALL = """
(call_expression function: (identifier) @callee)
(call_expression function: (field_expression field: (field_identifier) @callee))
(call_expression function: (qualified_identifier name: (identifier) @callee))
"""

CS_DECL = """
(method_declaration name: (identifier) @name parameters: (parameter_list) @params) @method
(class_declaration name: (identifier) @name) @class
(interface_declaration name: (identifier) @name) @interface
(struct_declaration name: (identifier) @name) @struct
(enum_declaration name: (identifier) @name) @enum
(constructor_declaration name: (identifier) @name parameters: (parameter_list) @params) @method
(field_declaration (variable_declaration (variable_declarator name: (identifier) @name))) @field
(property_declaration name: (identifier) @name) @field
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
