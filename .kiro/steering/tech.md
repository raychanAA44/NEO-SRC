---
inclusion: always
---

# Technical context

## Stack

- Python 3.10+ for the gateway, dispatcher, ledger, and operations scripts.
- Standard library HTTP server (`http.server`) with SSE streaming support.
- `sqlite3` by default; `pymysql` only when the ledger backend is MySQL.
- Browser console in plain HTML, CSS, and JavaScript; no frontend build step.
- JSON configuration under `运维区/configs/`; runtime data stays out of Git.

## Conventions

- Modules under `编码区/src/core/` own one concern each and are imported as
  `core.<module>`.
- SQLite SQL uses `?` placeholders. MySQL uses `%s`; the shared adapter
  translates placeholders instead of duplicating SQL in callers.
- Configuration reads must not crash the gateway on a malformed file; writes
  should be atomic where the file is shared.
- Tests live in `编码区/tests/` and run with `unittest` discovery from the
  repository root.
- Response text and code comments are written in Chinese; identifiers and
  public API names stay in English.

## Constraints

- Do not add a dependency when the standard library already covers the need.
- Keep changes bounded: one task should touch the smallest set of files that
  delivers a verifiable result.
- Never commit credentials, real upstream hostnames, ledger databases, logs, or
  historical screenshots.
- Routing behavior is covered by `test_task_lock_dispatcher.py` and
  `test_model_attributes.py`; keep those green.
