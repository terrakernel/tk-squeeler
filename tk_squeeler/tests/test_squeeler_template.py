from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Command
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestSqueelerTemplate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runner_group = cls.env['res.groups'].create({'name': 'Squeeler Template Runner'})
        cls.runner = new_test_user(cls.env, 'squeeler_runner', groups='base.group_user')
        cls.runner.group_ids = [Command.link(cls.runner_group.id)]
        cls.outsider = new_test_user(cls.env, 'squeeler_outsider', groups='base.group_user')
        cls.Template = cls.env['tk.squeeler.template']
        cls.template = cls.Template.create({
            'name': 'Users by login',
            'code': 'users_by_login',
            'query': "SELECT id, login FROM res_users WHERE login = %(login)s AND %(uid)s > 0 ORDER BY id LIMIT %(n)s",
            'group_ids': [Command.link(cls.runner_group.id)],
            'param_ids': [
                Command.create({'name': 'login', 'type': 'str', 'required': True}),
                Command.create({'name': 'n', 'type': 'int', 'default': '5'}),
            ],
        })

    def setUp(self):
        super().setUp()
        self.enterContext(self.registry_test_mode())

    def _run(self, user, code='users_by_login', params=None, **kwargs):
        return self.Template.with_user(user).run(code, params, **kwargs)

    def test_run(self):
        result = self._run(self.runner, params={'login': 'squeeler_runner'})
        self.assertEqual(result['columns'], ['id', 'login'])
        self.assertEqual(result['rows'], [[self.runner.id, 'squeeler_runner']])
        self.env.invalidate_all()
        log = self.env['tk.squeeler.log'].search([('user_id', '=', self.runner.id)])
        self.assertEqual((log.state, log.template_id, log.template_code), ('done', self.template, 'users_by_login'))

    def test_code_lookup_is_case_insensitive(self):
        self.assertEqual(self._run(self.runner, 'Users_By_Login ', {'login': 'admin'})['columns'], ['id', 'login'])

    def test_access(self):
        with self.assertRaises(AccessError):
            self._run(self.outsider, params={'login': 'admin'})
        with self.assertRaises(AccessError):
            self._run(self.runner, 'no_such_template')

    def test_refusal_is_logged(self):
        try:
            self._run(self.outsider, 'no_such_template', {'login': 'admin'})
        except AccessError:
            pass
        else:
            self.fail("AccessError not raised")
        self.env.invalidate_all()
        log = self.env['tk.squeeler.log'].search([('user_id', '=', self.outsider.id)])
        self.assertEqual((log.state, log.template_code, log.query), ('failed', 'no_such_template', False))

    def test_params(self):
        with self.assertRaises(ValidationError):
            self._run(self.runner, params={})  # login is required
        with self.assertRaises(ValidationError):
            self._run(self.runner, params={'login': 'admin', 'typo': 1})
        with self.assertRaises(ValidationError):
            self._run(self.runner, params={'login': 'admin', 'uid': 1})  # reserved
        with self.assertRaises(ValidationError):
            self._run(self.runner, params={'login': 'admin', 'n': 'many'})
        with self.assertRaises(ValidationError):
            self._run(self.runner, params=['admin'])

    def test_save_time_checks(self):
        group = [Command.link(self.runner_group.id)]
        with self.assertRaises(ValidationError):
            self.Template.create({'name': 'x', 'code': 'two', 'group_ids': group, 'query': "SELECT 1; SELECT 2"})
        with self.assertRaises(ValidationError):
            self.Template.create({'name': 'x', 'code': 'undeclared', 'group_ids': group, 'query': "SELECT %(a)s"})
        with self.assertRaises(ValidationError):
            self.Template.create({
                'name': 'x', 'code': 'unused', 'group_ids': group, 'query': "SELECT 1",
                'param_ids': [Command.create({'name': 'a', 'type': 'int'})],
            })
        with self.assertRaises(ValidationError):
            self.Template.create({'name': 'x', 'code': 'stray', 'group_ids': group, 'query': "SELECT 'a%'"})
        with self.assertRaises(ValidationError):
            self.Template.create({'name': 'x', 'code': '1bad', 'group_ids': group, 'query': "SELECT 1"})

    def test_duplicate_code(self):
        vals = {
            'name': 'Again', 'code': 'USERS_BY_LOGIN', 'query': "SELECT 1",
            'group_ids': [Command.link(self.runner_group.id)],
        }
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            self.Template.create(vals)

        # Archiving frees the code for a replacement, and run() picks the active one.
        self.template.action_archive()
        replacement = self.Template.create(vals)
        self.assertEqual(replacement.code, 'users_by_login')
        self.assertEqual(self._run(self.runner)['rows'], [[1]])

        # The archived version cannot come back while the code is taken.
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            self.template.action_unarchive()

    def test_copy_gets_a_free_code(self):
        first = self.template.copy()
        second = self.template.copy()
        self.assertEqual(first.code, 'users_by_login_copy')
        self.assertEqual(second.code, 'users_by_login_copy_2')
        self.assertEqual(first.param_ids.mapped('name'), ['login', 'n'])
