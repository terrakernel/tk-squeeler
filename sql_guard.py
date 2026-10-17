"""Lexical checks on raw SQL, kept free of Odoo imports so they test standalone."""
from __future__ import annotations

import re

_DOLLAR_TAG = re.compile(r'\$(?:[A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$')
_IDENT_CHAR = re.compile(r'[A-Za-z0-9_$\x80-￿]')


def is_single_statement(query: str) -> bool:
    """Return True when ``query`` holds exactly one SQL statement.

    psycopg2 sends text over the simple query protocol, which runs every
    ``;``-separated statement it is given. A ``COMMIT`` smuggled in the middle
    would end the READ ONLY transaction and run the rest in a fresh read-write
    one, so ``execute_read`` cannot rely on ``SET TRANSACTION READ ONLY`` alone.

    This walks the text the way PostgreSQL's lexer does for quoting purposes:
    ``--`` and nested ``/* */`` comments, ``'...'`` and ``E'...'`` strings,
    ``"..."`` identifiers and ``$tag$...$tag$`` bodies are skipped, and any
    ``;`` outside them must be followed by nothing but whitespace or comments.
    Anything it cannot close, it rejects.
    """
    i, n = 0, len(query)
    terminated = False
    seen_token = False
    while i < n:
        c = query[i]

        if c.isspace():
            i += 1
            continue

        if query.startswith('--', i):
            newline = query.find('\n', i)
            i = n if newline < 0 else newline + 1
            continue

        if query.startswith('/*', i):
            depth, i = 1, i + 2
            while i < n and depth:
                if query.startswith('/*', i):
                    depth, i = depth + 1, i + 2
                elif query.startswith('*/', i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                return False
            continue

        if terminated:
            return False

        if c == ';':
            terminated = True
            i += 1
            continue

        seen_token = True

        if c == "'":
            backslash_escapes = (
                i > 0 and query[i - 1] in 'eE'
                and (i < 2 or not _IDENT_CHAR.match(query[i - 2]))
            )
            i += 1
            while i < n:
                if backslash_escapes and query[i] == '\\':
                    i += 2
                elif query.startswith("''", i):
                    i += 2
                elif query[i] == "'":
                    break
                else:
                    i += 1
            if i >= n:
                return False
            i += 1
            continue

        if c == '"':
            i += 1
            while i < n:
                if query.startswith('""', i):
                    i += 2
                elif query[i] == '"':
                    break
                else:
                    i += 1
            if i >= n:
                return False
            i += 1
            continue

        if c == '$' and (i == 0 or not _IDENT_CHAR.match(query[i - 1])):
            tag = _DOLLAR_TAG.match(query, i)
            if tag:
                close = query.find(tag.group(), tag.end())
                if close < 0:
                    return False
                i = close + len(tag.group())
                continue

        i += 1

    return seen_token


_PERCENT = re.compile(r'%(%|\(([A-Za-z_][A-Za-z0-9_]*)\)s)?')


def named_placeholders(query: str) -> set[str]:
    """Return the names of the ``%(name)s`` placeholders in ``query``.

    psycopg2 substitutes on the raw text, inside string literals and comments
    too, so this scans the raw text the same way. Any ``%`` that is neither
    ``%%`` nor ``%(name)s`` makes psycopg2 fail at run time, so it raises
    ``ValueError`` here instead. That includes positional ``%s``.
    """
    names = set()
    for match in _PERCENT.finditer(query):
        if match.group(1) is None:
            raise ValueError(f"stray '%' at position {match.start()}: write a literal '%' as '%%'")
        if match.group(2):
            names.add(match.group(2))
    return names
