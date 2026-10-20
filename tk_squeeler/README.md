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
PostgreSQL reports a write or DDL statement sent to `execute_read` as a syntax
error at its keyword, so the message then ends with a hint to use
`execute_write`. The same hint follows a read-only rejection such as
`nextval()`.

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

## Calling through ODXProxy

[ODXProxy](https://odxproxy.io) is a JSON-RPC proxy in front of Odoo. Every
Squeeler method is a plain model method, so it is reached the same way as any
other: through `call_method` on v1, or by method name on v2. Odoo 20 supports
both. Prefer v2 for new code: it survives the move to Odoo 22, which drops
`/jsonrpc`.

Two keys are involved, and they are never the same value:

- the **proxy key** goes in the `x-api-key` HTTP header;
- the **Odoo key** goes in the body as `odoo_instance.api_key`. It must be an
  Odoo API key (scope `rpc`) of a user who holds the Squeeler groups, or the
  groups of the templates it runs. See [Granting access](#granting-access).

### v1: `POST /api/odoo/execute`

`action` is always `call_method`, `fn_name` is the Squeeler method, `params`
holds the positional arguments and `keyword` the named ones. Careful with the
name clash: the envelope's `params` is the argument list, while the method's
own `params` argument goes inside it or inside `keyword`.

```json
{
  "id": "req-1",
  "action": "call_method",
  "model_id": "tk.squeeler",
  "fn_name": "execute_read",
  "params": ["SELECT id, name FROM res_partner WHERE id = %s", [7]],
  "keyword": {"limit": 50},
  "odoo_instance": {"url": "https://erp.example.com", "db": "prod", "user_id": 2, "api_key": "<odoo key>"}
}
```

Other calls change only `model_id`, `fn_name`, `params` and `keyword`:

| Call | `model_id` | `fn_name` | `params` | `keyword` |
|---|---|---|---|---|
| Raw write | `tk.squeeler` | `execute_write` | `["UPDATE res_partner SET ref = %(ref)s WHERE id = %(id)s", {"ref": "X1", "id": 7}]` | |
| Run a template | `tk.squeeler.template` | `run` | `["partner_balance"]` | `{"params": {"date_to": "2026-09-30"}, "limit": 100, "expected_checksum": "9f2c..."}` |
| List templates | `tk.squeeler.template` | `catalog` | `[]` | |
| Describe a template | `tk.squeeler.template` | `describe` | `["partner_balance"]` | |

### v2: `POST /v2/odoo/execute`

`method` is the Squeeler method and every argument is named in `kwargs`, using
the Python parameter names (`query`, `params`, `limit`, `code`,
`expected_checksum`). There is no `user_id`. All Squeeler methods are
`@api.model`, so never send `ids`: Odoo rejects it with code `422`.

```json
{
  "id": "req-2",
  "model_id": "tk.squeeler.template",
  "method": "run",
  "kwargs": {
    "code": "partner_balance",
    "params": {"date_to": "2026-09-30", "partner_ids": [7, 9]},
    "limit": 100,
    "expected_checksum": "9f2c...",
    "context": {"allowed_company_ids": [1, 3]}
  },
  "odoo_instance": {"url": "https://erp.example.com", "db": "prod", "api_key": "<odoo key>"}
}
```

| Call | `model_id` | `method` | `kwargs` |
|---|---|---|---|
| Raw read | `tk.squeeler` | `execute_read` | `{"query": "SELECT id, name FROM res_partner WHERE id = %s", "params": [7], "limit": 50}` |
| Raw write | `tk.squeeler` | `execute_write` | `{"query": "UPDATE res_partner SET ref = %(ref)s WHERE id = %(id)s", "params": {"ref": "X1", "id": 7}}` |
| List templates | `tk.squeeler.template` | `catalog` | `{}` |
| Describe a template | `tk.squeeler.template` | `describe` | `{"code": "partner_balance"}` |

`context.allowed_company_ids` (in `kwargs` on v2, in `keyword` on v1) is what
fills the `%(company_ids)s` server parameter, limited to the user's companies.

### Python SDK

With [`terrakernel-odxproxyclient`](https://github.com/terrakernel):

```python
from terrakernel.odxproxyclient import ODXProxyClient

with ODXProxyClient(base_url="https://proxy.example.com", api_key="<proxy key>") as client:
    odoo = client.for_instance_v2(url="https://erp.example.com", db="prod", api_key="<odoo key>")

    entry = odoo.call_method("tk.squeeler.template", "describe",
                             kwargs={"code": "partner_balance"})
    result = odoo.call_method("tk.squeeler.template", "run", kwargs={
        "code": "partner_balance",
        "params": {"date_to": "2026-09-30"},
        "expected_checksum": entry["checksum"],
    })
    for row in result["rows"]:
        print(dict(zip(result["columns"], row)))

    # v1 equivalent
    v1 = client.for_instance(url="https://erp.example.com", db="prod", user_id=2, api_key="<odoo key>")
    v1.call_method("tk.squeeler", "execute_read",
                   params=["SELECT id, login FROM res_users WHERE active"], keyword={"limit": 10})
```

### Responses and errors

On success, the envelope's `result` is the method's return value, such as
`{"columns": [...], "rows": [...], "rowcount": 1, "truncated": false}`.

A Squeeler error still comes back as **HTTP 200**, with `error` set instead of
`result`, so check `error` first. Branch on `error.data.name` and show
`error.data.message`, which carries PostgreSQL's text for a failed query:

| Raised by Squeeler | `error.data.name` | `error.code` on v2 |
|---|---|---|
| Missing group, or unknown/forbidden template | `odoo.exceptions.AccessError` | `403` |
| Bad input, several statements, checksum mismatch | `odoo.exceptions.ValidationError` | `422` |
| PostgreSQL error (syntax, timeout, write in read mode) | `odoo.exceptions.UserError` | `422` |

On v1, `error.code` is `0` and `error.message` is just `Odoo Server Error`;
the details are in `error.data` as above.

### Timeouts

ODXProxy waits 15 seconds for Odoo by default, while
`tk_squeeler.statement_timeout_ms` defaults to 30 seconds. Send an
`x-request-timeout` header (in seconds; `timeout_secs=` in the Python SDK)
above the statement timeout for long queries. Otherwise the proxy answers
`-32003` (HTTP 504) while the query keeps running in Odoo. Retry a `-32003`
only for reads: an `execute_write` may already have committed.

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
