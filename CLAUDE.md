# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An Odoo 20.0 addon, `tk_squeeler` (depends only on `base`), that exposes raw PostgreSQL to external API clients (`execute_kw`, JSON-2, ODXProxy `call_method`). `README.md` is the user-facing spec for the API, guarantees and limits. Keep it in sync when behaviour changes.

The repo root *is* the module, and Odoo takes the module name from the folder name. The checkout must therefore be named `tk_squeeler` (`git clone git@github.com:terrakernel/tk-squeeler.git tk_squeeler`): a default `tk-squeeler` clone is not a valid module name, and every `tk_squeeler.*` xmlid would break. The *parent* folder of the checkout goes on `--addons-path`.

## Running tests

There is no build, lint config or test runner in the repo. Tests run under an Odoo 20.0 server:

```sh
# whole module
odoo-bin -d <db> --addons-path=<odoo>/addons,<parent of the tk_squeeler checkout> -i tk_squeeler --test-enable --stop-after-init
# one class or one method (Odoo test-tags syntax: [module][/Class][.method])
odoo-bin ... -u tk_squeeler --test-tags /tk_squeeler:TestSqueelerTemplate
odoo-bin ... -u tk_squeeler --test-tags /tk_squeeler:TestSqueeler.test_read_returns_rows
```

The model tests are tagged `post_install, -at_install`. `test_sql_guard.py` and `test_schema.py` are `BaseCase` tests of pure-Python modules.

## Architecture

**Single execution pipeline.** Every SQL call, whether raw or from a template, goes through `tk.squeeler._execute_logged()` in `models/squeeler.py`. It takes an `authorize` callback. For raw SQL that callback only checks the group. For templates (`_run_template`) it also resolves the template, re-runs `_check_query()`, verifies `expected_checksum`, coerces params, merges server params, and returns `(query, params, row_cap)` to run instead. After authorization: `_validate` → `_clamp_limit` → read or write path → audit log. `tk.squeeler.template.run()` is a thin delegate back into `tk.squeeler`.

**Read path** (`_execute_read_only`, also used by `_describe_columns`): a fresh `registry.cursor(readonly=True)` (the replica if configured), `SET TRANSACTION READ ONLY`, a transaction-local `statement_timeout` via `set_config(..., true)`, then `DECLARE tk_squeeler NO SCROLL CURSOR FOR <query>` + `FETCH limit+1`. It always rolls back and never commits. Read-only safety comes from PostgreSQL, not from parsing SQL. The only lexical guard is `sql_guard.is_single_statement()`, which exists because psycopg2's simple query protocol would otherwise run a smuggled `; COMMIT; ...`. Don't weaken it, and don't concatenate anything after the user query.

**Write path** (`_execute_on_request_cursor`): `flush_all()`, then a savepoint on the request cursor, then the timeout is set and restored around the statement, then `invalidate_all()`.

**Audit-log transaction rules** (`tk.squeeler.log`): a success is logged on the request env, so a rolled-back write leaves no entry. A failure (`psycopg2.Error`, `AccessError`, `ValidationError`) is logged by `_log_on_new_cursor`, because the request transaction is about to roll back. psycopg2 errors are re-raised as `UserError`. `catalog`/`describe` are not logged.

**Templates** (`models/squeeler_template.py`): `tk.squeeler.template` + `tk.squeeler.template.param`. Codes are normalized lowercase, with a partial unique index on active codes (archive and re-create to replace). `_get_runnable` searches as sudo and returns the same `AccessError` for a missing template and a forbidden one, so callers can't probe which codes exist. Keep that property. Reserved server params (`uid`, `company_id`, `company_ids`) come from `_server_params()` and can't be passed by callers. `_checksum()` hashes the query and the param declarations. `describe()` plans the query with `_sample_params()` and fetches zero rows to get column types. SQL text is never returned to API callers.

**Pure-Python modules**: `sql_guard.py` (statement splitting and `%(name)s` extraction, quote/comment/dollar-quote aware) and `schema.py` (JSON Schema for params/columns, used as MCP `inputSchema`). Both must stay free of Odoo imports so they can be tested standalone.

## Test gotchas

- Model tests call `self.enterContext(self.registry_test_mode())` in `setUp` so the separate cursors (read path, failure log) land on the test transaction.
- To assert that a call fails *and* was logged, use the `_raises_and_logs` try/except helper, not `assertRaises`. Odoo wraps `assertRaises` in a savepoint that rolls back the failure log entry.

## Conventions

- Settings are `ir.config_parameter` keys `tk_squeeler.max_rows` and `tk_squeeler.statement_timeout_ms` (in `data/squeeler_data.xml`, `noupdate`).
- Groups: `tk_squeeler.group_squeeler_read` and `group_squeeler_write` (write implies read). Settings admins don't get them implicitly.
- The branch name matches the Odoo series (`20.0`). The module version is `20.0.x.y.z`.
