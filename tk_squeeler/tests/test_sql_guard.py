from odoo.tests.common import BaseCase

from ..sql_guard import is_single_statement, named_placeholders


class TestSqlGuard(BaseCase):

    def test_accepts_one_statement(self):
        for query in [
            "SELECT 1",
            "SELECT 1;",
            "SELECT 1;  -- trailing comment",
            "SELECT 1; /* trailing */ ",
            "SELECT ';' AS semi",
            "SELECT 'it''s; fine'",
            "SELECT E'back\\'slash; fine'",
            'SELECT 1 AS "a;b"',
            "SELECT $$; still body$$",
            "SELECT $fn$ ; $$ ; $fn$",
            "SELECT 1 -- ; comment\n",
            "SELECT /* ; /* nested ; */ ; */ 1",
            "SELECT %s, %(name)s",
            "SELECT $1",
            "WITH x AS (SELECT 1) SELECT * FROM x",
        ]:
            with self.subTest(query=query):
                self.assertTrue(is_single_statement(query))

    def test_rejects_more_than_one(self):
        for query in [
            "SELECT 1; SELECT 2",
            "SELECT 1; COMMIT; DELETE FROM res_partner",
            "SELECT 1;;",
            "SELECT 'a'; DELETE FROM x",
            "SELECT $$x$$; DELETE FROM x",
            "SELECT 1 /* c */ ; DELETE FROM x",
            "SELECT 'it''s'; DELETE FROM x",
            "SELECT E'\\''; DELETE FROM x",
        ]:
            with self.subTest(query=query):
                self.assertFalse(is_single_statement(query))

    def test_rejects_what_it_cannot_close(self):
        for query in [
            "SELECT 'open",
            'SELECT "open',
            "SELECT $$open",
            "SELECT 1 /* open",
            # Without the E prefix a backslash is literal, so the string closes
            # at the second quote and the third one opens a new, unclosed one.
            "SELECT 'a\\''; DELETE FROM x",
        ]:
            with self.subTest(query=query):
                self.assertFalse(is_single_statement(query))

    def test_rejects_empty(self):
        for query in ["", "   ", ";", "-- only a comment", "/* only */"]:
            with self.subTest(query=query):
                self.assertFalse(is_single_statement(query))

    def test_named_placeholders(self):
        self.assertEqual(
            named_placeholders("SELECT %(a)s, %(b_2)s WHERE x LIKE 'a%%' AND y = %(a)s"),
            {'a', 'b_2'},
        )
        self.assertEqual(named_placeholders("SELECT 1"), set())

    def test_named_placeholders_rejects_stray_percent(self):
        for query in ["SELECT %s", "SELECT 'a%'", "SELECT %(a)d", "SELECT 1 %"]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                named_placeholders(query)
