---
inclusion: auto
description: How HUB NEO work is split across Kiro agents, reviewed, and verified.
---

# Kiro workflow

HUB NEO changes are handled as bounded tasks rather than one long session.

## Roles

- **Architecture and task framing (main session).** Defines the slice, the
  files a task may touch, the acceptance criteria, and what is explicitly out
  of scope. Reads `AGENTS.md`, `PROJECT.md`, and the current state first.
- **`gateway-engineer`.** Implements one slice: reads the target source and
  its tests, makes the smallest change, runs the focused suites, and reports
  conclusion, files, commands, and remaining risks.
- **`code-reviewer`.** Reviews the diff against module boundaries, dependency
  and credential rules, routing invariants, and test coverage. Reports only
  high-confidence findings.
- **Additional project roles** live in `.agents/agents/` for client runtime,
  log diagnosis, and security audit; they receive the same bounded handoff.

## Handoff contract

Each task states: goal, allowed files, forbidden areas, acceptance command,
and evidence required. An agent that cannot run a check says "unverified"
instead of claiming a pass. A failed change returns to a runnable baseline
before the next attempt.

## Applied example

The Phase 1 optimization (2026-11-01) followed this flow:

1. The main session scoped the duplication to the database wrapper and JSON
   I/O, and limited the first migration to `accounts.py`.
2. `gateway-engineer` added `core/db_adapter.py` and `core/config_io.py`,
   migrated `accounts.py`, and removed the private `_Con`, `_rows`, and `_db`
   helpers.
3. Verification ran the accounts suites and the focused gateway suites before
   the change was accepted.

The spec for that work is in `.kiro/specs/code-optimization-phase1/`.
