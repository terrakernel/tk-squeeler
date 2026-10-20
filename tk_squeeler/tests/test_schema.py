from odoo.tests.common import BaseCase

from ..schema import column_schema, input_schema


class TestSchema(BaseCase):

    def test_input_schema(self):
        result = input_schema([
            {'name': 'login', 'type': 'str', 'required': True, 'default': None, 'description': 'User login'},
            {'name': 'n', 'type': 'int', 'required': False, 'default': 5, 'description': ''},
            {'name': 'ids', 'type': 'int_list', 'required': False, 'default': None, 'description': ''},
            {'name': 'at', 'type': 'datetime', 'required': True, 'default': None, 'description': 'Cut-off'},
        ])
        self.assertEqual(result['required'], ['login', 'at'])
        self.assertFalse(result['additionalProperties'])
        props = result['properties']
        self.assertEqual(props['login'], {'type': 'string', 'description': 'User login'})
        self.assertEqual(props['n'], {'type': 'integer', 'default': 5})
        self.assertEqual(props['ids'], {'type': ['array', 'null'], 'items': {'type': 'integer'}})
        self.assertEqual(props['at']['type'], 'string')
        self.assertIn('UTC', props['at']['description'])

    def test_required_with_default_is_optional_for_the_caller(self):
        result = input_schema([{'name': 'n', 'type': 'int', 'required': True, 'default': 5, 'description': ''}])
        self.assertEqual(result['required'], [])

    def test_column_schema(self):
        self.assertEqual(column_schema('integer', 'N'), {'type': ['integer', 'null']})
        self.assertEqual(column_schema('numeric', 'N'), {'type': ['number', 'null']})
        self.assertEqual(column_schema('boolean', 'B'), {'type': ['boolean', 'null']})
        self.assertEqual(column_schema('character varying', 'S'), {'type': ['string', 'null']})
        self.assertEqual(column_schema('date', 'D'), {'type': ['string', 'null'], 'format': 'date'})
        self.assertIn('UTC', column_schema('timestamp without time zone', 'D')['description'])
        self.assertEqual(column_schema('jsonb', 'U'), {'description': 'Any JSON value'})
        self.assertEqual(column_schema('bytea', 'U')['contentEncoding'], 'base64')
        self.assertEqual(
            column_schema('integer[]', 'A', 'integer', 'N'),
            {'type': ['array', 'null'], 'items': {'type': ['integer', 'null']}},
        )
