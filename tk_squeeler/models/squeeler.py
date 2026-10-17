from __future__ import annotations

import base64
import json
import logging
import time
from datetime import date, datetime, timezone

import psycopg2

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..sql_guard import is_single_statement

_logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 10000
DEFAULT_STATEMENT_TIMEOUT_MS = 30000


class TkSqueeler(models.AbstractModel):
    _name = 'tk.squeeler'
    _description = 'Raw SQL Executor'

    @api.model
    def execute_read(self, query, params=None, limit=None):
        """Run one read-only query and return its result set.

        The query runs on its own cursor (on the read replica when
        ``db_replica_*`` is configured), in a transaction opened READ ONLY and
        always rolled back, so PostgreSQL rejects any write (including
        ``nextval()``) whatever the SQL says. It does not see uncommitted work
        from the caller's transaction.

        Rows are pulled through a server-side cursor, so only ``limit + 1`` of
        them ever reach Odoo. That limits the query to what ``DECLARE CURSOR``
        accepts: ``SELECT``, ``VALUES``, ``TABLE`` and ``WITH ... SELECT``.

        :param str query: one SQL statement; ``%s`` or ``%(name)s`` placeholders
        :param params: list or dict of values for the placeholders
        :param int limit: rows to return, capped by ``tk_squeeler.max_rows``
        :return: ``{'columns', 'rows', 'rowcount', 'truncated'}``
        """
        return self._run('read', 'tk_squeeler.group_squeeler_read', query, params, limit)

    @api.model
    def execute_write(self, query, params=None):
        """Run one statement on the request cursor, in the caller's transaction.

        This bypasses the ORM entirely: no access rules, no record rules, no
        constraints, no computed field recomputation, no ``mail.thread``
        tracking. The ORM cache is invalidated afterwards so the rest of the
        request sees the new rows. A ``RETURNING`` clause comes back as rows,
        capped by ``tk_squeeler.max_rows``.

        :param str query: one SQL statement; ``%s`` or ``%(name)s`` placeholders
        :param params: list or dict of values for the placeholders
        :return: ``{'columns', 'rows', 'rowcount', 'truncated'}``
        """
        return self._run('write', 'tk_squeeler.group_squeeler_write', query, params, None)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run(self, mode, group_xmlid, query, params, limit):
        return self._execute_logged(mode, query, params, limit, authorize=lambda: self._check_group(group_xmlid))

    @api.model
    def _run_template(self, code, params, limit):
        """Back end of tk.squeeler.template.run(): templates are read-only."""
        state = {'template': self.env['tk.squeeler.template'], 'code': code}

        def authorize():
            template = state['template'] = state['template']._get_runnable(code)
            # Constraints guard edits made through the form; this also catches
            # parameter rows changed directly, before psycopg2 hits a KeyError.
            template._check_query()
            values = {**template._coerce_params(params), **self._server_params()}
            return template.query, values, template.max_rows

        return self._execute_logged('read', None, params, limit, authorize=authorize, template_state=state)

    def _execute_logged(self, mode, query, params, limit, authorize, template_state=None):
        """Authorize, run and audit one statement.

        ``authorize`` raises AccessError or ValidationError to refuse the call.
        For a template it returns ``(query, params, row_cap)`` to run instead.
        """
        started = time.monotonic()
        try:
            prepared = authorize()
            row_cap = None
            if prepared:
                query, params, row_cap = prepared
            self._validate(query, params)
            limit = self._clamp_limit(limit, row_cap)
            if mode == 'read':
                result = self._execute_read_only(query, params, limit)
            else:
                result = self._execute_on_request_cursor(query, params, limit)
        except (psycopg2.Error, AccessError, ValidationError) as e:
            message = str(e).strip()
            # The request transaction is about to roll back, taking anything
            # written on it with it, so failures are logged on a cursor of their own.
            self._log_on_new_cursor(mode, query, params, started, error=message, template_state=template_state)
            _logger.info("tk_squeeler: %s by uid %s failed: %s", mode, self.env.uid, message)
            if isinstance(e, (AccessError, ValidationError)):
                raise
            raise UserError(message) from None

        # Successful writes are logged in the same transaction as the write,
        # so the log never claims a change that was rolled back.
        self._log(self.env, mode, query, params, started, rowcount=result['rowcount'], template_state=template_state)
        return result

    def _execute_read_only(self, query, params, limit):
        cr = self.env.registry.cursor(readonly=True)
        try:
            cr.execute("SET TRANSACTION READ ONLY")
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [str(self._statement_timeout())])
            # is_single_statement() has already ruled out anything after the
            # query that could escape this prefix.
            # Failures are recorded in the audit log; skip Odoo's "bad query" ERROR.
            cr.execute("DECLARE tk_squeeler NO SCROLL CURSOR FOR " + query, params or None, log_exceptions=False)
            cr.execute("FETCH FORWARD %s FROM tk_squeeler", [limit + 1])
            return self._collect(cr, limit)
        finally:
            # Never commit: rolling back also undoes any session-level
            # set_config() the query may have issued on this pooled connection.
            cr.rollback()
            cr.close()

    def _execute_on_request_cursor(self, query, params, limit):
        cr = self.env.cr
        self.env.flush_all()
        with cr.savepoint():
            cr.execute("SELECT current_setting('statement_timeout')")
            previous_timeout = cr.fetchone()[0]
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [str(self._statement_timeout())])
            cr.execute(query, params or None, log_exceptions=False)
            result = self._collect(cr, limit)
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [previous_timeout])
        self.env.invalidate_all()
        return result

    @api.model
    def _collect(self, cr, limit):
        if cr.description is None:
            return {'columns': [], 'rows': [], 'rowcount': cr.rowcount, 'truncated': False}
        columns = [column.name for column in cr.description]
        rows = cr.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = [[self._to_json(value) for value in row] for row in rows[:limit]]
        return {'columns': columns, 'rows': rows, 'rowcount': len(rows), 'truncated': truncated}

    @api.model
    def _to_json(self, value):
        """Convert a psycopg2 value into something every JSON encoder accepts.

        ``numeric`` needs no case: Odoo registers a global typecaster that
        already returns it as ``float``.
        """
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, datetime):
            if value.tzinfo:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            return fields.Datetime.to_string(value)
        if isinstance(value, date):
            return fields.Date.to_string(value)
        if isinstance(value, (bytes, memoryview)):
            return base64.b64encode(bytes(value)).decode()
        if isinstance(value, (list, tuple)):
            return [self._to_json(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._to_json(item) for key, item in value.items()}
        # time, timedelta, UUID, ranges, inet, ...
        return str(value)

    # ------------------------------------------------------------------
    # Checks and settings
    # ------------------------------------------------------------------

    @api.model
    def _check_group(self, group_xmlid):
        if not self.env.user.has_group(group_xmlid):
            raise AccessError(_("You are not allowed to run SQL through Squeeler."))

    @api.model
    def _validate(self, query, params):
        if not isinstance(query, str) or not query.strip():
            raise ValidationError(_("The query must be a non-empty string."))
        if params is not None and not isinstance(params, (list, tuple, dict)):
            raise ValidationError(_("Parameters must be a list or a dict."))
        if not is_single_statement(query):
            raise ValidationError(_("Exactly one SQL statement is allowed per call."))

    @api.model
    def _max_rows(self):
        return max(self.env['ir.config_parameter'].sudo().get_int('tk_squeeler.max_rows', DEFAULT_MAX_ROWS), 1)

    @api.model
    def _clamp_limit(self, limit, row_cap=None):
        max_rows = self._max_rows()
        if row_cap:
            max_rows = min(max_rows, row_cap)
        if limit is None:
            return max_rows
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError(_("The limit must be a positive integer."))
        return min(limit, max_rows)

    @api.model
    def _server_params(self):
        """Template parameters taken from the caller, never from the request body.

        ``env.companies`` honours ``allowed_company_ids`` from the request
        context, and Odoo refuses companies the user does not belong to.
        """
        return {
            'uid': self.env.uid,
            'company_id': self.env.company.id,
            'company_ids': self.env.companies.ids,
        }

    @api.model
    def _statement_timeout(self):
        return max(self.env['ir.config_parameter'].sudo().get_int('tk_squeeler.statement_timeout_ms', DEFAULT_STATEMENT_TIMEOUT_MS), 1)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    @api.model
    def _log(self, env, mode, query, params, started, rowcount=0, error=None, template_state=None):
        template_vals = {}
        if template_state:
            code = template_state['code']
            template_vals = {
                'template_id': template_state['template'].id,
                'template_code': code if isinstance(code, str) else repr(code),
            }
        env['tk.squeeler.log'].sudo().create({
            **template_vals,
            'user_id': self.env.uid,
            'mode': mode,
            'query': query if isinstance(query, str) or query is None else repr(query),
            'params': json.dumps(params, default=str) if params is not None else False,
            'state': 'failed' if error else 'done',
            'rowcount': rowcount,
            'duration_ms': (time.monotonic() - started) * 1000,
            'error': error or False,
        })

    @api.model
    def _log_on_new_cursor(self, mode, query, params, started, error, template_state=None):
        try:
            with self.env.registry.cursor() as cr:
                self._log(self.env(cr=cr), mode, query, params, started, error=error, template_state=template_state)
        except Exception:
            _logger.exception("tk_squeeler: could not record a failed %s in the audit log", mode)
