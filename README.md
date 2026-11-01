# HUB NEO — AI Routing Gateway

HUB NEO is a local AI gateway with model routing, task locks, dispatch traces, a usage ledger, and an operations console. Kiro Crew was used as the development tool, including a multi-agent workflow for bounded tasks.

## Routing

Explicit model IDs pass through the dispatcher unchanged. An `auto/*` group activates policy-based selection: existing task locks are reused; new decisions filter active candidates, enforce vision requirements, optionally match task domains and tiers, and select by priority. Task locks are in memory and reset on restart. Model upstream routing maps selected models to configured channels. The dispatch ledger may persist traces without storing prompts, response bodies or secrets.

## Kiro workspace

`.kiro/` contains the workspace assets used with Kiro:

- `steering/` — product, technical, structure and workflow context loaded for every session.
- `specs/code-optimization-phase1/` — requirements, design and task list for the shared database adapter and JSON I/O refactor.
- `agents/` — custom `gateway-engineer` and `code-reviewer` agent definitions used for bounded implementation and review tasks.

## Local startup

Run from this directory with Python 3.10 or newer:

```bash
python 运维区/scripts/start_gateway.py
```

Open http://127.0.0.1:8888/. The published configuration deliberately uses SQLite and inactive example upstreams. Configure your own upstreams and credentials through environment variables before making model requests; do not expose administrative endpoints to the public internet. A working upstream or model response is not included.

Run focused routing tests:

```bash
python -m unittest discover -s 编码区/tests -p 'test_task_lock_dispatcher.py'
python -m unittest discover -s 编码区/tests -p 'test_model_attributes.py'
```

## Demo

[Routing demo video](demo/HUB-NEO-routing-demo.mp4) shows the architecture, isolated dispatcher results, an agent-workflow diagram and an isolated console recording. It is not a live upstream inference demonstration.

## Public snapshot

This is a sanitized source snapshot: runtime databases, credentials, logs, deployment details and historical screenshots have been omitted. Example upstreams are placeholders. The owner has not specified a project license in this snapshot.
