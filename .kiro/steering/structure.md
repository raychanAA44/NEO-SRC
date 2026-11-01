---
inclusion: always
---

# Repository structure

```text
编码区/
  src/
    server.py            HTTP entry point, routing table, proxy handling
    core/                dispatcher, task locks, ledger, pricing, accounts
    core/db_adapter.py   shared SQLite/MySQL connection adapter
    core/config_io.py    shared JSON read/write helpers
    kwsa_lite/           lightweight gateway variant
  tests/                 unittest suites (routing, ledger, accounts, server)
  scripts/               migration and maintenance scripts
运维区/
  configs/               example configuration (sanitized in the public repo)
  scripts/               start_gateway.py, deployment and verification helpers
.agents/agents/          project role definitions (engineer, reviewer, ...)
.kiro/                   Kiro workspace assets (steering, specs, agents)
```

## Module rules

- `server.py` reads environment configuration at import time; new settings go
  through `core/paths.py` or `core/config_io.py` rather than ad-hoc reads.
- Database access goes through `core/db_adapter.py` so SQLite and MySQL share
  one connection, transaction, and row-normalization path.
- File-backed configuration goes through `core/config_io.py`; callers decide
  whether a missing file is fatal (`load_json_or_fail`) or optional
  (`load_json_safe`).
- A feature that changes routing must ship with a dispatcher test.
- The public snapshot contains source, tests, example configuration, Kiro
  assets, and the demo video only. Runtime data, logs, deployment records, and
  historical screenshots are excluded.
