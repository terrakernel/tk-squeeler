from __future__ import annotations

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class TkSqueelerLog(models.Model):
    _name = 'tk.squeeler.log'
    _description = 'Squeeler Audit Log'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', string='User', required=True, ondelete='restrict', index=True)
    mode = fields.Selection(
        [('read', 'Read'), ('write', 'Write')],
        required=True,
        index=True,
    )
    query = fields.Text(help="Empty when a template call was refused before its query was known.")
    template_id = fields.Many2one('tk.squeeler.template', string='Template', ondelete='set null', index=True)
    template_code = fields.Char(help="The code the caller asked for, kept even if the template is deleted.")
    params = fields.Text(help="The placeholder values, as JSON.")

    state = fields.Selection(
        [('done', 'Done'), ('failed', 'Failed')],
        required=True,
        index=True,
    )
    rowcount = fields.Integer(string='Rows', help="Rows returned, or rows affected by a write without RETURNING.")
    duration_ms = fields.Float(string='Duration (ms)', digits=(16, 1))
    error = fields.Text()

    executed_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)

    @api.model
    def _gc_logs(self, days=90):
        """Drop audit log rows older than ``days``."""
        limit_date = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.search([('executed_at', '<', limit_date)])
        count = len(stale)
        if count:
            stale.unlink()
            _logger.info("tk_squeeler: garbage collected %s audit log entries", count)
        return count
