# HUB NEO — Code Optimization Phase 1 — Tasks

## 1. Shared database adapter

- [x] 1.1 Add `编码区/src/core/db_adapter.py` with `UnifiedConnection`,
      `transact`, `normalize_rows`, and connection factories.
- [x] 1.2 Translate `?` placeholders to `%s` for MySQL and split
      `executescript` statements on MySQL.
- [x] 1.3 Roll back on exception and always close the raw connection.

## 2. Shared JSON I/O

- [x] 2.1 Add `编码区/src/core/config_io.py` with `load_json_safe`.
- [x] 2.2 Add `save_json_atomic` with temporary-file replacement and optional
      `.bak` backup.
- [x] 2.3 Add `load_json_or_fail` and `update_json_field`.

## 3. Migrate the accounts module

- [x] 3.1 Remove `_Con`, `_rows`, and `_db` from `accounts.py`.
- [x] 3.2 Replace database blocks with `db_adapter.transact`.
- [x] 3.3 Replace row normalization with `db_adapter.normalize_rows`.
- [x] 3.4 Add explicit commits after inserts, updates, and deletes.
- [x] 3.5 Keep the `vendor_creds.v1` credential format and table schema.

## 4. Verification

- [x] 4.1 Compile the three touched modules.
- [x] 4.2 Run `test_accounts_checkin.py` and `test_accounts_crypt.py`
      (13 tests, pass).
- [x] 4.3 Run gateway smoke, security headers, start-gateway configuration,
      task-lock dispatcher, and model attributes (57 tests, pass).
- [x] 4.4 Confirm no new dependency was added.

## 5. Follow-up (not part of this change)

- [ ] 5.1 Migrate `local_ledger.py` `_SqliteStore` / `_MySQLStore` to the
      shared adapter.
- [ ] 5.2 Migrate the remaining JSON configuration call sites to
      `config_io`.
- [ ] 5.3 Add dedicated unit tests for `db_adapter` and `config_io`,
      including MySQL placeholder translation and rollback behavior.
