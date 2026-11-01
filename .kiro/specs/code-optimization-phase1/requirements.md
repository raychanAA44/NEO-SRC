# HUB NEO — Code Optimization Phase 1 — Requirements

Status: implemented in `编码区/src/core/db_adapter.py`,
`编码区/src/core/config_io.py`, and the migrated `编码区/src/core/accounts.py`.

## Overview

Several core modules carried their own copies of the same two patterns: a
SQLite/MySQL connection wrapper, and JSON file read/write code. Phase 1
extracts both patterns into shared modules and migrates the accounts module
first, without changing observable gateway behavior and without adding a
dependency.

## R1 — Shared database adapter

**As an** implementer, **I want** one SQLite/MySQL adapter, **so that**
placeholder translation, transaction handling, and row normalization are
fixed in one place.

Acceptance criteria (EARS):

1. WHEN a caller opens a transaction through the adapter, THE SYSTEM SHALL
   yield a connection wrapper.
2. WHEN the underlying connection is a MySQL connection, THE SYSTEM SHALL
   translate `?` placeholders to `%s` before executing a statement.
3. WHEN `executescript` runs against a MySQL connection, THE SYSTEM SHALL
   split the script on `;` and execute each non-empty statement.
4. WHEN an exception is raised inside the transaction body, THE SYSTEM SHALL
   roll back and re-raise the exception.
5. WHEN the transaction body ends, THE SYSTEM SHALL close the underlying
   connection even if closing raises.
6. WHEN rows are normalized, THE SYSTEM SHALL return tuples for both
   dictionary rows and tuple rows, preserving column order.

## R2 — Shared JSON I/O

**As an** operator, **I want** configuration reads that do not crash the
gateway and writes that cannot leave a half-written file, **so that** a bad
file is a contained failure.

Acceptance criteria:

1. WHEN a JSON file is missing, unreadable, or malformed,
   `load_json_safe` SHALL return the supplied default instead of raising.
2. WHEN `save_json_atomic` writes a document, THE SYSTEM SHALL write a
   temporary file and replace the target atomically.
3. WHEN `backup=True` and the target exists, THE SYSTEM SHALL copy the
   previous file to `<name>.bak` before replacement.
4. WHEN a caller requires a valid file, `load_json_or_fail` SHALL raise
   `FileNotFoundError` or `ValueError`.
5. WHEN `update_json_field` applies a partial update, THE SYSTEM SHALL keep
   unrelated keys.

## R3 — Migrate the accounts module

**As a** maintainer, **I want** `accounts.py` to use the shared adapter,
**so that** the duplicated `_Con`, `_rows`, and `_db` code disappears.

Acceptance criteria:

1. WHEN accounts persist or read rows, THE SYSTEM SHALL use the shared
   adapter and keep the existing `vendor_creds.v1` encryption format.
2. WHEN a check-in does not succeed, THE SYSTEM SHALL NOT change
   `last_checkin`.
3. WHEN an account row is deleted, THE SYSTEM SHALL delete exactly one row
   and report a not-found result for an unknown id.
4. WHEN the plaintext-credential migration runs, THE SYSTEM SHALL seal each
   legacy row once and remain idempotent.

## R4 — No behavioral regression

WHEN the focused suites run, accounts, gateway smoke, security headers,
start-gateway configuration, task-lock dispatcher, and model attributes
SHALL pass.

## R5 — No new dependency

THE SYSTEM SHALL use only `sqlite3`, the optional `pymysql`, and the Python
standard library.
