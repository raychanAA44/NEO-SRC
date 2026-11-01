---
inclusion: always
---

# Product context

HUB NEO is a local-first AI routing gateway and operations console. It sits
between client tools and one or more model providers so a small team can see
where a request went, why that model was chosen, and what it cost.

## Users

- Developers and operators who run several model providers side by side.
- Small teams that need a readable, local record of model usage without
  sending prompts or credentials to a third-party dashboard.

## Goals

- Route explicit model IDs unchanged; route `auto/*` groups by policy.
- Keep task-level routing decisions stable through in-memory task locks.
- Record dispatch traces and usage in a local ledger.
- Expose models, channels, requests, and budget state in one console.
- Stay runnable with the Python standard library plus optional MySQL support.

## Boundaries

- No prompt text, response bodies, or secrets in dispatch traces.
- No claim of measured provider health, price, or success rate without a real
  observation; unavailable data stays clearly unavailable.
- Administrative endpoints are local-only by default and must not be exposed to
  the public internet without an explicit deployment review.
- Recovery and audit features default to shadow mode unless configured
  otherwise; documentation must not describe them as enforced by default.
