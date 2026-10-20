# ADR-001: Build the 2.0 core in the existing Python engine, not a Next.js rewrite

**Status:** accepted, 17 Oct 2026. **Context:** the 2.0 spec recommends Next.js + TypeScript with pure TS domain modules, and also says to inspect first and not refactor destructively.

## Decision

Implement the 2.0 domain core as a new pure-Python package `tuitionpilot/treasury/` (standard library only), next to the working v0.1 engine. The UI stays a static site that renders recorded, reproducible runs of the core.

## Reasons

* The submission deadline is 20 Oct 2026, 23:59 Beijing time. A stack migration would spend most of the remaining time re-creating working, tested behaviour.
* The domain rules the spec cares about (minor units, hard constraints, policy, approval binding, reservations, ledger) are language-independent. Pure modules with no I/O port to TypeScript one-to-one later.
* The existing engine has 39 passing tests and the demo depends on it.

## Consequences and trade-offs

* There is no server, so "server-side policy" means the policy runs inside the engine process, not in the browser. The site never makes decisions; it only shows recorded outputs. Interactive calculators on the site are labelled as educational and are not part of the decision path.
* Persistence is in memory. Unique idempotency keys and limit reservations are protected by locks inside one process. This is **not** a substitute for database constraints across processes. A PostgreSQL (or PocketBase after a spike) store is required before any multi-user pilot.
* A future Next.js UI should call these modules through an API, or a TypeScript port validated against the same QA fixtures.

## Revisit when

A multi-user pilot, real persistence or a server deployment is planned.
