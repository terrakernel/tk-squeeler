"""JSON Schema for template parameters and result columns.

Kept free of Odoo imports so it tests standalone. The schemas are what an LLM
tool definition (e.g. an MCP ``inputSchema``) or a code generator consumes.
"""
from __future__ import annotations

DATETIME_HINT = "UTC, 'YYYY-MM-DD HH:MM:SS'"

_PARAM_SCHEMAS = {
    'int': {'type': 'integer'},
    'float': {'type': 'number'},
    'str': {'type': 'string'},
    'bool': {'type': 'boolean'},
    'date': {'type': 'string', 'format': 'date'},
    'datetime': {'type': 'string', 'description': DATETIME_HINT},
    'int_list': {'type': 'array', 'items': {'type': 'integer'}},
    'str_list': {'type': 'array', 'items': {'type': 'string'}},
}

_INTEGER_TYPES = {'smallint', 'integer', 'bigint', 'oid'}


def _nullable(schema: dict) -> dict:
    return {**schema, 'type': [schema['type'], 'null']} if 'type' in schema else schema


def param_schema(type_: str, *, nullable: bool, description: str | None = None, default=None) -> dict:
    """Schema of one declared parameter."""
    schema = dict(_PARAM_SCHEMAS[type_])
    if nullable:
        schema = _nullable(schema)
    if description:
        hint = schema.get('description')
        schema['description'] = f'{description} ({hint})' if hint else description
    if default is not None:
        schema['default'] = default
    return schema


def input_schema(params: list[dict]) -> dict:
    """Schema of the ``params`` object ``run()`` accepts.

    :param params: dicts with ``name``, ``type``, ``required``, ``default``
        (decoded JSON or None) and ``description``
    """
    return {
        'type': 'object',
        'properties': {
            param['name']: param_schema(
                param['type'],
                # A parameter that is neither required nor defaulted is sent as NULL.
                nullable=not param['required'] and param['default'] is None,
                description=param['description'],
                default=param['default'],
            )
            for param in params
        },
        'required': [param['name'] for param in params if param['required'] and param['default'] is None],
        'additionalProperties': False,
    }


def column_schema(pg_type: str, category: str, element_type: str | None = None, element_category: str | None = None) -> dict:
    """Schema of a result column, as ``run()`` serializes it.

    :param pg_type: ``format_type()`` of the column, e.g. ``'bigint'``
    :param category: ``pg_type.typcategory``, e.g. ``'N'``
    :param element_type: for arrays, ``format_type()`` of the element
    :param element_category: for arrays, the element's ``typcategory``
    """
    if category == 'A' and element_type:
        return _nullable({'type': 'array', 'items': column_schema(element_type, element_category or 'X')})
    if pg_type in _INTEGER_TYPES:
        return _nullable({'type': 'integer'})
    if category == 'N':
        # numeric too: Odoo's typecaster returns it as float.
        return _nullable({'type': 'number'})
    if category == 'B':
        return _nullable({'type': 'boolean'})
    if pg_type == 'date':
        return _nullable({'type': 'string', 'format': 'date'})
    if pg_type.startswith('timestamp'):
        return _nullable({'type': 'string', 'description': DATETIME_HINT})
    if pg_type in ('json', 'jsonb'):
        return {'description': 'Any JSON value'}
    if pg_type == 'bytea':
        return _nullable({'type': 'string', 'contentEncoding': 'base64'})
    # text, varchar, uuid, time, interval, inet, ranges, ...
    return _nullable({'type': 'string'})
