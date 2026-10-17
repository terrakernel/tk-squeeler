# tk-squeeler

Odoo addon that exposes raw PostgreSQL (`cr.execute()`) to API clients through
two model methods.

- **Odoo version:** 20.0
- **Module:** `tk_squeeler`
- **License:** LGPL-3

## API

Both methods live on the abstract model `tk.squeeler` and are `@api.model`, so
they need no record ids. They work over `execute_kw`, JSON-2 and ODXProxy's
`call_method`.

| Method | Group | Runs on | Transaction |
|---|---|---|---|
| `execute_read(query, params=None, limit=None)` | Squeeler: Read SQL | its own cursor, on the replica if `db_replica_*` is set | `READ ONLY`, always rolled back |
| `execute_write(query, params=None)` | Squeeler: Write SQL (implies Read) | the request cursor | the caller's, committed with the request |

`params` is a list for `%s` placeholders or a dict for `%(name)s`. Values are
bound by psycopg2, never formatted into the string. With no `params`, a literal
`%` needs no escaping. With `params`, write it as `%%`.

Both return:

```json
{"columns": ["id", "login"], "rows": [[2, "admin"]], "rowcount": 1, "truncated": false}
```

A statement that returns no rows, such as an `UPDATE` without `RETURNING`,
comes back with `columns: []`, `rows: []` and `rowcount` set to the number of
rows affected. Values are made JSON-safe: `numeric` becomes a float (Odoo
does this for every query, so very large or high-precision values lose
precision),
timestamps become UTC `YYYY-MM-DD HH:MM:SS`, `bytea` becomes base64, and
anything else unusual (UUID, interval, inet, ranges) becomes its string form.

Errors are raised, not returned: `AccessError` (missing group),
`ValidationError` (bad input, more than one statement) or `UserError` carrying
PostgreSQL's message (syntax error, timeout, write attempted in read mode).

### Example (`execute_kw`)

```python
models.execute_kw(db, uid, api_key, 'tk.squeeler', 'execute_read',
                  ["SELECT id, name FROM res_partner WHERE id = %s", [7]],
                  {'limit': 50})
```

## Query templates

Settings administrators save read-only queries under Settings → Squeeler →
Query Templates. API clients run them by code:

```python
# execute_kw / ODXProxy v1 call_method
models.execute_kw(db, uid, api_key, 'tk.squeeler.template', 'run', ['partner_balance'],
                  {'params': {'date_to': '2026-09-30', 'partner_ids': [7, 9]}, 'limit': 100})
```

```http
POST /json/2/tk.squeeler.template/run          (JSON-2 / ODXProxy v2)
{"code": "partner_balance", "params": {"date_to": "2026-09-30", "partner_ids": [7, 9]}}
```

The result has the same `{columns, rows, rowcount, truncated}` shape as
`execute_read`, and runs through the same read-only path: separate `READ ONLY`
transaction, server-side cursor, statement timeout, audit log.

**Access.** A template lists its allowed groups, and a caller needs any one of
them. The Squeeler SQL groups are not involved, so an integration user can run
a few vetted queries without raw SQL access. A missing template and a forbidden
one return the same `AccessError`, so callers cannot probe which codes exist.

**Parameters** are passed in one `params` dict, by name. Each one is declared
on the template with a type (`int`, `float`, `str`, `bool`, `date`,
`datetime`, `int_list`, `str_list`), an optional `required` flag and an
optional JSON default (`10`, `"draft"`, `[1, 2]`). Values are converted before
the query runs. An unknown name, a missing required value or a wrong type
raises `ValidationError`. Use `= ANY(%(name)s)` to match a list.

**Server-filled parameters.** `%(uid)s`, `%(company_id)s` and
`%(company_ids)s` can be used in any template without being declared. They come
from the caller's session (`company_ids` honours `allowed_company_ids` in the
request context, limited to the user's companies) and cannot be passed in
`params`. Use them to scope a template to the caller.

**Checked on save.** A template must be a single statement, every
`%(name)s` it uses must be declared or server-filled, every declared parameter
must be used, and a literal `%` must be written `%%`. psycopg2 substitutes
inside string literals too, so the `%%` rule applies everywhere. The checks run
again on every call.

**Codes.** A code is stored lowercase (`[a-z][a-z0-9_]*`), and lookups ignore
case and surrounding spaces. A partial unique index allows only one *active*
template per code:

- to replace a template, archive it and create a new one with the same code;
  the old version stays readable, and in the audit log;
- unarchiving a template whose code has been taken over is refused;
- *Duplicate* gives the copy `<code>_copy`, then `<code>_copy_2`, and so on.

The code is the stable key across databases cloned from the same template
database. Record ids are not.

**Row cap.** A template's *Row Cap* lowers `tk_squeeler.max_rows` for that
template. It can't raise it.

### Discovery: `catalog()` and `describe(code)`

Both are on `tk.squeeler.template`, require the same groups as `run`, and are
`@api.readonly`: on JSON-2 they get a read-only cursor, or the replica.

- `catalog()` lists every active template the caller may run: code, name,
  description, parameters, `input_schema`, `max_rows` and `checksum`.
- `describe(code)` returns the same entry plus `columns`, each with its name,
  PostgreSQL type and JSON Schema.

```json
{
  "code": "users_by_login",
  "name": "Users by login",
  "description": "...",
  "params": [{"name": "login", "type": "str", "required": true, "default": null, "description": ""}],
  "server_params": ["uid"],
  "input_schema": {"type": "object", "properties": {"login": {"type": "string"}},
                   "required": ["login"], "additionalProperties": false},
  "columns": [{"name": "id", "pg_type": "integer", "json_schema": {"type": ["integer", "null"]}}],
  "max_rows": 10000,
  "checksum": "9f2c..."
}
```

`input_schema` is plain JSON Schema, so it can be used as is as an MCP tool's
`inputSchema` or fed to a code generator. Columns are not stored: `describe`
plans the query behind a server-side cursor with typed sample values and
fetches zero rows, so no data is read and a schema change is visible on the
next call. A template that no longer plans raises `UserError`.

The SQL is never returned. A template explains itself through its
*description* and its parameters' descriptions, so write those for a reader who
can't see the query, such as an LLM picking a tool.

`checksum` hashes the query and the parameter declarations. It is identical
across cloned databases while the template is unchanged, so clients and the
proxy can use it to detect drift or as a cache key.

Pass it back as `run(..., expected_checksum=...)` to pin a caller to the
version it was built against. If the template has changed since, the call is
refused with a `ValidationError` naming both checksums, rather than returning
rows of a different shape. Generated SDK code should always send it. Without
it, `run` simply uses the current version.

`describe` and `catalog` calls are not written to the audit log. They read no
data.

## Guarantees and limits

**One statement per call.** psycopg2 uses PostgreSQL's simple query protocol,
which runs every `;`-separated statement it is given. A `COMMIT` in the middle
would end the read-only transaction and run the rest read-write. `sql_guard.py`
lexes the text, handling comments, strings, quoted identifiers and dollar
quotes, and rejects anything after the first `;`.

**Read mode is enforced by PostgreSQL, not by parsing.** The query runs inside
`SET TRANSACTION READ ONLY`, through `DECLARE ... CURSOR FOR <query>` /
`FETCH`, so:

- writes, DDL and `nextval()` are rejected by the server;
- only `SELECT`, `VALUES`, `TABLE` and `WITH ... SELECT` are accepted
  (`EXPLAIN` and `SHOW` are not);
- only `limit + 1` rows ever leave the database, so the cap also bounds memory;
- the query does not see uncommitted work from the caller's transaction and,
  on a replica, may lag slightly behind the primary.

Read-only is not a sandbox. Every table can be read, including `res_users`
password hashes, `res_users_apikeys` and `ir_config_parameter` secrets.
Functions that act outside the transaction, such as `dblink` or
`pg_terminate_backend` if the database role allows them, still work. Treat the
Read group as database-wide read access.

**Write mode skips the ORM.** No access rules, record rules, constraints,
computed-field recomputation or chatter tracking. The ORM cache is invalidated
afterwards. The statement runs in a savepoint, so a failure leaves the
request's cursor usable.

**Settings** (`ir.config_parameter`, created `noupdate`):

| Key | Default | |
|---|---|---|
| `tk_squeeler.max_rows` | `10000` | Upper bound for `limit` and for `RETURNING` rows |
| `tk_squeeler.statement_timeout_ms` | `30000` | `statement_timeout`, applied per call |

## Audit log

Every call is recorded in `tk.squeeler.log`, under Settings → Squeeler →
SQL Audit Log (system administrators only). Template calls also record the
template and the code that was asked for. That includes denied calls, rejected input and
failed queries. Each entry holds the user, mode, query, params, row count,
duration and error.

- A successful **write** is logged in the same transaction as the write, so a
  rolled-back request leaves no "done" entry behind.
- **Failures** are logged on a separate cursor, because the request transaction
  is about to roll back.

A daily cron drops entries older than 90 days.

## Granting access

Neither group is implied by Settings administrators. Add them to specific users,
typically the integration user whose API key ODXProxy uses.
