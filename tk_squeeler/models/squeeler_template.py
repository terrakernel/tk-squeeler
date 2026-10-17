from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime

import psycopg2

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .. import schema
from ..sql_guard import is_single_statement, named_placeholders

CODE_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$')
PARAM_NAME_PATTERN = re.compile(r'^[a-z_][a-z0-9_]*$')

# Filled in by the server from the caller's environment on every run. They can
# be used in any template without being declared, and a caller cannot set them.
RESERVED_PARAMS = frozenset({'uid', 'company_id', 'company_ids'})

PARAM_TYPES = [
    ('int', 'Integer'),
    ('float', 'Float'),
    ('str', 'Text'),
    ('bool', 'Boolean'),
    ('date', 'Date'),
    ('datetime', 'Datetime'),
    ('int_list', 'List of integers'),
    ('str_list', 'List of texts'),
]


def normalize_code(code):
    return code.strip().lower() if isinstance(code, str) else code


class TkSqueelerTemplate(models.Model):
    _name = 'tk.squeeler.template'
    _description = 'Squeeler Query Template'
    _order = 'code, id'
    _rec_name = 'name'

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        index=True,
        help="What API clients pass to run(). Lowercase letters, digits and underscores, "
             "starting with a letter. Only one active template may hold a given code.",
    )
    active = fields.Boolean(default=True)
    description = fields.Text(
        help="What the template returns and when to use it. API clients and LLM agents read it "
             "through catalog() and describe(), so write it for someone who cannot see the SQL.",
    )
    query = fields.Text(
        required=True,
        help="One read-only SQL statement. Use %(name)s for parameters and %% for a literal %. "
             "uid, company_id and company_ids are always available.",
    )
    param_ids = fields.One2many('tk.squeeler.template.param', 'template_id', string='Parameters', copy=True)
    group_ids = fields.Many2many(
        'res.groups',
        'tk_squeeler_template_res_groups_rel', 'template_id', 'group_id',
        string='Allowed Groups',
        required=True,
        help="Members of any of these groups may run the template. They do not need the Squeeler SQL groups.",
    )
    max_rows = fields.Integer(
        string='Row Cap',
        help="Rows this template may return at most. Zero means the global tk_squeeler.max_rows; "
             "a value above it is ignored.",
    )

    # Archiving a template frees its code: the old version stays readable, in
    # the audit log too, and a new template can take the code over.
    _code_active_uniq = models.UniqueIndex(
        '(code) WHERE active',
        "An active template already uses this code. Archive it first, or pick another code.",
    )
    _max_rows_positive = models.Constraint(
        'CHECK (max_rows >= 0)',
        "The row cap cannot be negative.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'code' in vals:
                vals['code'] = normalize_code(vals['code'])
        return super().create(vals_list)

    def write(self, vals):
        if 'code' in vals:
            vals['code'] = normalize_code(vals['code'])
        return super().write(vals)

    def copy_data(self, default=None):
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        if 'code' in default:
            return vals_list
        taken = set(self.with_context(active_test=False).search([]).mapped('code'))
        for template, vals in zip(self, vals_list):
            vals['code'] = code = self._free_copy_code(template.code, taken)
            taken.add(code)
            if 'name' not in default:
                vals['name'] = _("%s (copy)", template.name)
        return vals_list

    @api.model
    def _free_copy_code(self, code, taken):
        candidate = f'{code}_copy'
        index = 2
        while candidate in taken:
            candidate = f'{code}_copy_{index}'
            index += 1
        return candidate

    @api.constrains('code')
    def _check_code(self):
        for template in self:
            if not CODE_PATTERN.match(template.code or ''):
                raise ValidationError(_(
                    "Template code %r must start with a letter and contain only lowercase letters, digits and underscores.",
                    template.code,
                ))

    @api.constrains('query', 'param_ids')
    def _check_query(self):
        for template in self:
            if not is_single_statement(template.query or ''):
                raise ValidationError(_("Template %s must contain exactly one SQL statement.", template.code))
            try:
                used = named_placeholders(template.query)
            except ValueError as e:
                raise ValidationError(_("Template %(code)s: %(error)s", code=template.code, error=e)) from None
            declared = set(template.param_ids.mapped('name'))
            undeclared = used - declared - RESERVED_PARAMS
            if undeclared:
                raise ValidationError(_(
                    "Template %(code)s uses parameters that are not declared: %(names)s",
                    code=template.code, names=', '.join(sorted(undeclared)),
                ))
            unused = declared - used
            if unused:
                raise ValidationError(_(
                    "Template %(code)s declares parameters it never uses: %(names)s",
                    code=template.code, names=', '.join(sorted(unused)),
                ))

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @api.model
    def run(self, code, params=None, limit=None):
        """Run the active template ``code`` and return its result set.

        :param str code: the template's code
        :param dict params: values for the declared parameters, by name
        :param int limit: rows to return, capped by the template and by
            ``tk_squeeler.max_rows``
        :return: ``{'columns', 'rows', 'rowcount', 'truncated'}``
        """
        return self.env['tk.squeeler']._run_template(code, params, limit)

    @api.model
    @api.readonly
    def catalog(self):
        """List the active templates the caller may run, without their result columns.

        Meant for discovery, e.g. by an LLM agent choosing a tool: each entry
        has the code, name, description, parameters and ``input_schema``.
        Call :meth:`describe` for the result columns.
        """
        templates = self.sudo().search([('group_ids', 'in', self.env.user.all_group_ids.ids)])
        return [template._describe() for template in templates]

    @api.model
    @api.readonly
    def describe(self, code):
        """Describe the active template ``code``: what :meth:`run` accepts and returns.

        :return: dict with ``code``, ``name``, ``description``, ``params``,
            ``server_params``, ``input_schema`` (JSON Schema of ``params``),
            ``columns`` (name, PostgreSQL type and JSON Schema of each result
            column), ``max_rows`` and ``checksum``. The SQL itself is not exposed.
        """
        template = self._get_runnable(code)
        template._check_query()
        description = template._describe()
        squeeler = self.env['tk.squeeler']
        try:
            columns = squeeler._describe_columns(template.query, {**template._sample_params(), **squeeler._server_params()})
        except psycopg2.Error as e:
            raise UserError(_("Template %(code)s cannot be planned: %(error)s", code=template.code, error=str(e).strip())) from None
        description['columns'] = [
            {
                'name': name,
                'pg_type': pg_type,
                'json_schema': schema.column_schema(pg_type, category, element_type, element_category),
            }
            for name, pg_type, category, element_type, element_category in columns
        ]
        return description

    def _describe(self):
        self.ensure_one()
        params = [
            {
                'name': param.name,
                'type': param.type,
                'required': param.required,
                'default': json.loads(param.default) if param.default else None,
                'description': param.description or '',
            }
            for param in self.param_ids
        ]
        max_rows = self.env['tk.squeeler']._max_rows()
        return {
            'code': self.code,
            'name': self.name,
            'description': self.description or '',
            'params': params,
            'server_params': sorted(named_placeholders(self.query) & RESERVED_PARAMS),
            'input_schema': schema.input_schema(params),
            'max_rows': min(max_rows, self.max_rows) if self.max_rows else max_rows,
            'checksum': self._checksum(),
        }

    def _checksum(self):
        """Hash of everything that defines the template's behaviour.

        Equal across databases cloned from one another while the template is
        unchanged, so clients and the proxy can detect drift or key a cache on it.
        """
        self.ensure_one()
        definition = {
            'query': self.query,
            'params': [(param.name, param.type, param.required, param.default or None) for param in self.param_ids],
        }
        return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()

    def _sample_params(self):
        """Typed placeholder values for planning the query without real input."""
        self.ensure_one()
        return {param.name: param._sample_value() for param in self.param_ids}

    # ------------------------------------------------------------------
    # Helpers for tk.squeeler
    # ------------------------------------------------------------------

    @api.model
    def _get_runnable(self, code):
        """Return the active template ``code`` (as sudo) if the current user may run it."""
        if not isinstance(code, str):
            raise ValidationError(_("The template code must be a string."))
        template = self.sudo().search([('code', '=', normalize_code(code))], limit=1)
        # The same message whether the template is missing or forbidden, so
        # callers cannot probe which codes exist.
        if not template or not (template.group_ids & self.env.user.all_group_ids):
            raise AccessError(_("No template %r that you are allowed to run.", code))
        return template

    def _coerce_params(self, params):
        """Validate caller-supplied ``params`` against the declarations and return typed values."""
        self.ensure_one()
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValidationError(_("Template parameters must be a dict, by name."))
        reserved = RESERVED_PARAMS & params.keys()
        if reserved:
            raise ValidationError(_("These parameters are set by the server and cannot be passed: %s", ', '.join(sorted(reserved))))
        declared = {param.name: param for param in self.param_ids}
        unknown = params.keys() - declared.keys()
        if unknown:
            raise ValidationError(_(
                "Template %(code)s has no parameter %(names)s.",
                code=self.code, names=', '.join(sorted(map(str, unknown))),
            ))
        values = {}
        for name, param in declared.items():
            if name in params:
                values[name] = param._coerce(params[name])
            elif param.default:
                values[name] = param._coerce(json.loads(param.default))
            elif param.required:
                raise ValidationError(_("Template %(code)s requires parameter %(name)s.", code=self.code, name=name))
            else:
                values[name] = None
        return values


class TkSqueelerTemplateParam(models.Model):
    _name = 'tk.squeeler.template.param'
    _description = 'Squeeler Query Template Parameter'
    _order = 'template_id, sequence, id'

    template_id = fields.Many2one('tk.squeeler.template', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, help="Used in the query as %(name)s.")
    type = fields.Selection(PARAM_TYPES, required=True, default='str')
    required = fields.Boolean()
    default = fields.Char(help='A JSON value used when the caller omits the parameter, e.g. 10, "draft", [1, 2], "2026-01-31".')
    description = fields.Char()

    _name_uniq = models.Constraint(
        'UNIQUE (template_id, name)',
        "A template cannot declare the same parameter twice.",
    )

    @api.constrains('name', 'type', 'default')
    def _check_declaration(self):
        for param in self:
            if not PARAM_NAME_PATTERN.match(param.name or ''):
                raise ValidationError(_(
                    "Parameter name %r must contain only lowercase letters, digits and underscores, and not start with a digit.",
                    param.name,
                ))
            if param.name in RESERVED_PARAMS:
                raise ValidationError(_("%s is set by the server and cannot be declared.", param.name))
            if param.default:
                try:
                    param._coerce(json.loads(param.default))
                except (ValueError, ValidationError) as e:
                    raise ValidationError(_("Default for %(name)s is not valid: %(error)s", name=param.name, error=e)) from None

    def _sample_value(self):
        self.ensure_one()
        if self.default:
            return self._coerce(json.loads(self.default))
        return {
            'int': 0,
            'float': 0.0,
            'str': '',
            'bool': False,
            'date': date.today(),
            'datetime': datetime.now(),
            'int_list': [],
            'str_list': [],
        }[self.type]

    def _coerce(self, value):
        """Return ``value`` converted to this parameter's type, or raise ValidationError."""
        self.ensure_one()
        if value is None:
            if self.required:
                raise ValidationError(_("Parameter %s cannot be null.", self.name))
            return None
        try:
            return self._coerce_value(self.type, value)
        except (TypeError, ValueError):
            raise ValidationError(_(
                "Parameter %(name)s expects %(type)s, got %(value)r.",
                name=self.name, type=dict(PARAM_TYPES)[self.type], value=value,
            )) from None

    @api.model
    def _coerce_value(self, type_, value):
        if type_ == 'int':
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise TypeError
            return int(value)
        if type_ == 'float':
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise TypeError
            return float(value)
        if type_ == 'str':
            if not isinstance(value, str):
                raise TypeError
            return value
        if type_ == 'bool':
            if not isinstance(value, bool):
                raise TypeError
            return value
        # Odoo's parsers turn '' into None, which would slip past `required`.
        if type_ == 'date':
            if not isinstance(value, str) or not value:
                raise TypeError
            return fields.Date.to_date(value)
        if type_ == 'datetime':
            if not isinstance(value, str) or not value:
                raise TypeError
            return fields.Datetime.to_datetime(value)
        if type_ in ('int_list', 'str_list'):
            if not isinstance(value, list):
                raise TypeError
            return [self._coerce_value(type_.removesuffix('_list'), item) for item in value]
        raise ValueError(type_)
