from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged('post_install', '-at_install')
class TestSqueeler(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reader = new_test_user(cls.env, 'squeeler_reader', groups='base.group_user,tk_squeeler.group_squeeler_read')
        cls.writer = new_test_user(cls.env, 'squeeler_writer', groups='base.group_user,tk_squeeler.group_squeeler_write')
        cls.nobody = new_test_user(cls.env, 'squeeler_nobody', groups='base.group_user')
        cls.partner = cls.env['res.partner'].create({'name': 'Squeeler Before'})

    def setUp(self):
        super().setUp()
        # Route the separate cursors execute_read and the failure log open
        # back onto the test transaction, which is never committed.
        self.enterContext(self.registry_test_mode())

    def _raises_and_logs(self, exception, func, *args):
        # Not assertRaises: Odoo wraps it in a savepoint that would roll back
        # the failure log this module writes.
        try:
            func(*args)
        except exception:
            return
        self.fail(f"{exception.__name__} not raised")

    def _logs(self, user):
        self.env.invalidate_all()
        return self.env['tk.squeeler.log'].search([('user_id', '=', user.id)])

    def test_read_returns_rows(self):
        result = self.env['tk.squeeler'].with_user(self.reader).execute_read(
            "SELECT id, login FROM res_users WHERE id = %s", [self.reader.id],
        )
        self.assertEqual(result['columns'], ['id', 'login'])
        self.assertEqual(result['rows'], [[self.reader.id, 'squeeler_reader']])
        self.assertFalse(result['truncated'])
        log = self._logs(self.reader)
        self.assertEqual(log.state, 'done')
        self.assertEqual(log.rowcount, 1)

    def test_read_truncates_at_limit(self):
        result = self.env['tk.squeeler'].with_user(self.reader).execute_read(
            "SELECT generate_series(1, 5) AS n", limit=3,
        )
        self.assertEqual(result['rows'], [[1], [2], [3]])
        self.assertTrue(result['truncated'])

    def test_read_cannot_write(self):
        squeeler = self.env['tk.squeeler'].with_user(self.reader)
        self._raises_and_logs(UserError, squeeler.execute_read, "SELECT nextval('res_partner_id_seq')")
        self._raises_and_logs(UserError, squeeler.execute_read, "UPDATE res_partner SET name = 'x'")
        logs = self._logs(self.reader)
        self.assertEqual(len(logs), 2)
        self.assertEqual(set(logs.mapped('state')), {'failed'})

    def test_multiple_statements_rejected(self):
        with self.assertRaises(ValidationError):
            self.env['tk.squeeler'].with_user(self.writer).execute_write(
                "SELECT 1; COMMIT; DELETE FROM res_partner",
            )

    def test_access(self):
        self._raises_and_logs(AccessError, self.env['tk.squeeler'].with_user(self.nobody).execute_read, "SELECT 1")
        with self.assertRaises(AccessError):
            self.env['tk.squeeler'].with_user(self.reader).execute_write("SELECT 1")
        log = self._logs(self.nobody)
        self.assertEqual(log.state, 'failed')

    def test_write_is_visible_to_orm(self):
        self.assertEqual(self.partner.name, 'Squeeler Before')
        result = self.env['tk.squeeler'].with_user(self.writer).execute_write(
            "UPDATE res_partner SET name = %(name)s WHERE id = %(id)s RETURNING id",
            {'name': 'Squeeler After', 'id': self.partner.id},
        )
        self.assertEqual(result['rows'], [[self.partner.id]])
        self.assertEqual(self.partner.name, 'Squeeler After')

    def test_write_without_result_set(self):
        result = self.env['tk.squeeler'].with_user(self.writer).execute_write(
            "UPDATE res_partner SET name = name WHERE id = %s", [self.partner.id],
        )
        self.assertEqual(result['columns'], [])
        self.assertEqual(result['rowcount'], 1)
