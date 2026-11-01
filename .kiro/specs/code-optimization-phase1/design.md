# HUB NEO — Code Optimization Phase 1 — Design

## Context

Before this change, `core/accounts.py` owned a private connection wrapper
(`_Con`), a row normalizer (`_rows`), and a transaction context (`_db`).
`core/local_ledger.py` contains a second, larger copy of the same idea in
`_SqliteStore` and `_MySQLStore`. Separately, more than fifteen modules read
and write JSON configuration with hand-rolled `read_text` / `json.loads` /
`write_text` sequences; some writes are not atomic.

Phase 1 introduces two shared modules and migrates one caller
(`accounts.py`). The remaining migrations are explicitly out of scope.

## Components

### `core/db_adapter.py`

- `is_mysql_backend(connection)` — detects the MySQL driver by connection type.
- `UnifiedConnection` — wraps a raw connection:
  - `execute(sql, args)` translates `?` to `%s` for MySQL;
  - `executescript(sql)` runs natively on SQLite and splits statements on
    MySQL;
  - `commit()`, `rollback()`, `close()` delegate to the raw connection; close
    failures are swallowed so a closed file handle cannot mask a result.
- `transact(raw_connection)` — context manager: yields the wrapper, rolls back
  and re-raises on exception, always closes in `finally`.
- `normalize_rows(rows, mysql)` — converts dictionary rows and tuple rows to
  tuples in column order.
- `create_sqlite_connection(...)` / `create_mysql_connection(...)` — optional
  factories with the project defaults (30 s SQLite timeout, `utf8mb4`,
  `DictCursor`, `autocommit=False`).

### `core/config_io.py`

- `load_json_safe(path, default=None)` — returns `default` on missing file,
  OS error, decode error, malformed JSON, or a non-object top level.
- `save_json_atomic(path, data, *, backup=True, indent=2, ensure_ascii=False)`
  — creates the parent directory, optionally copies the previous file to
  `<name>.bak`, writes `<name>.tmp`, then replaces the target.
- `load_json_or_fail(path)` — strict variant; raises `FileNotFoundError` or
  `ValueError`.
- `update_json_field(path, updates)` — shallow merge followed by an atomic
  write.

## Transaction semantics

The adapter does **not** commit on success. Callers commit explicitly. This is
intentional: the old `_db` helper committed implicitly, which hid transaction
boundaries and made read-only blocks commit as a side effect. The accounts
migration therefore adds an explicit `con.commit()` after every write.

On exception the adapter rolls back and re-raises; `finally` closes the
connection. This matters on Windows, where an unclosed SQLite connection keeps
the database file locked and breaks test cleanup.

## Accounts migration

Removed from `accounts.py`: `_Con` (26 lines), `_rows` (3 lines), and `_db`
(15 lines). Added `from core import db_adapter`; replaced seven `with _db(...)`
blocks with `with db_adapter.transact(ledger.connect())`; replaced three
`_rows(...)` calls with `db_adapter.normalize_rows(..., mysql)`; added explicit
commits after inserts, updates, and deletes.

`_mysql(ledger)` remains in `accounts.py` because it inspects the ledger
configuration, not the connection. The credential format, table schema, and
public function signatures are unchanged.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| MySQL `DictCursor` row order differs from `SELECT` order | `normalize_rows` preserves insertion order; callers select explicit columns |
| A missed explicit commit would silently drop a write | Every write block in `accounts.py` commits; covered by accounts tests |
| Close failure could mask a business result | `close()` swallows driver errors |
| `.bak` files accumulate | Only the latest backup is kept per file; cleanup is deferred |
| Partial migration leaves two adapter paths | `local_ledger.py` migration is tracked as the next task |

## Out of scope

- Migrating `local_ledger.py`.
- Migrating the fifteen JSON call sites.
- Type annotations and splitting `HubNeoHandler`.
