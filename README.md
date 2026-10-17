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

Every call is recorded in `tk.squeeler.log`, under Settings → SQL Audit Log
(system administrators only). That includes denied calls, rejected input and
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
