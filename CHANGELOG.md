# Changelog

## 0.3.2 — 2026-10-19 (site)
- "Plan my payment": the 2.0 optimizer ported to the browser. Your bill, balance, cushion, rent and sponsor dates in; 20 plans, hard limits, cheapest safe plan, money-by-date chart, exact approval with a SHA-256 fingerprint and a sandbox lifecycle out. Identical to `treasury/optimizer.py` on 1,500 random scenarios / 30,000 plans (`evidence/planner_parity.json`, `tools/parity/planner_parity.mjs`, `treasury/planner_api.py`).
- Home: "Before you pay tuition: a 6-point check" and tap-to-explain terms.

## 0.3.1 — 2026-10-19 (site)
- "Aigerim’s story": interactive 8-chapter story with mini-games driven by recorded engine runs, and a plain-English glossary.
- "Student cases": Aigerim (route vs deadline), Mira (changed bank details, live field comparison), Li (parent pays from China, documents).
- "Check my payment": browser port of `treasury/country.py` eligibility and deadline projection; matches the Python engine on 7,776 input combinations (`evidence/js_parity.json`, `tools/parity/parity.mjs`).
- Home: "Who are you?" audience picker and story teaser.

## 0.3.0 — 2026-10-19 (Master spec v2.1: Country Intelligence, China-first)

### Added
- `treasury/country.py`: Country Intelligence Layer with versioned, immutable-after-use profiles, sources with scope and freshness, corridor resolver, deterministic method eligibility (ELIGIBLE / INELIGIBLE / UNKNOWN / STALE / MANUAL_REVIEW), readiness statuses and deadline projection. China-first pack with two separate corridors (FROM_ABROAD_TO_CHINA, FROM_CHINA_TO_ABROAD), all DEMO FIXTURES; public university pages listed as NOT_VERIFIED research starting points.
- `treasury/tracking.py`: payment lifecycle where submitted is not credited, reconciliation with exact and fuzzy matches, manual resolution with actor and evidence, provider idempotency with IDEMPOTENCY_CONFLICT.
- `treasury/evidence.py`: Evidence Pack manifest, verification (VALID / INVALID / INCOMPLETE), PII redaction, export blocked when a token would leak.
- `treasury/evaluation.py`: Evaluation Lab, 16 cases from Appendix C, INFRA_ERROR never counted as PASS, release gate.
- `python -m tuitionpilot.treasury china` writes `evidence/china_corridors.json`, `payment_lifecycle.json`, `evidence_pack.json`, `evaluation_run.json`.
- 29 tests in `tests/test_country.py` (103 in total). `docs/GAP_MATRIX_2_1.md`.
- Site: new "China corridors" page, payment timeline and Evidence Pack on the plan page, Evaluation Lab on Results, updated hero copy and caps.

## 0.2.0 — 2026-10-17 (TuitionPilot 2.0 core)

### Added
- `tuitionpilot/treasury/`: minor-unit money and directed rates; route quotes with timestamp, TTL and itemised all-in cost; business-day calendar with cut-off and time zone; liquidity timeline with reserve; deadline-constrained optimizer with INFEASIBLE results; Tuition Shield with immutable beneficiary versions; policy POL-001…POL-023; exact-action approvals bound to a fingerprint; concurrency-safe cap reservations; idempotent sandbox executor; ledger with redaction, checkpoints and JSONL/CSV export; stress and counterfactual lab; 9 typed tools; flagship demo and replay CLI.
- 35 tests mapped to workbook QA-001…QA-026 (`tests/test_treasury.py`); evidence in `evidence/`.
- Docs: AS-IS report, ADR-001 (keep the Python core), ADR-002 (fixture dates and units), 2.0 report, backlog workbook with evidence.
- Demo site: "Payment plan 2.0" page and a persistent SIMULATION bar.

### Changed
- Site and deck wording no longer implies guaranteed savings, fraud elimination or an untamperable log.

### Unchanged
- The v0.1 engine (`tuitionpilot/*.py`) and its 39 tests.

## 0.1.0 — 2026-10-17
- First engine: rails, FX planner, policy tiers, guards, hash-chained audit, scenarios, backtest, demo site.
