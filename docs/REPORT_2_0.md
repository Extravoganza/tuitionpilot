# TuitionPilot 2.0: implementation report (Appendix B)

17 October 2026. Everything below runs in **SIMULATION. No real money moves.** All routes, quotes, payees and balances are fictional fixtures.

## 1. Repository baseline and stack

* Baseline: commit `ec77771` (v0.1.0). Python ≥ 3.10, standard library only, 39 tests, static demo site. There was no Next.js app, no database and no server. Full audit: [AS-IS.md](AS-IS.md).
* Decision: build the 2.0 core as a new pure-Python package `tuitionpilot/treasury/` next to the working v0.1 engine; no Next.js rewrite before the 20 Oct deadline ([ADR-001](adr/ADR-001-keep-python-core.md)).
* Workbook conflicts and fixture choices (dates, R4, liquidity, units, caps): [ADR-002](adr/ADR-002-fixture-dates-and-units.md).

## 2. Requirements completed (file paths, tests)

| Module | Files | Tests / evidence | Status |
|---|---|---|---|
| Money in minor units, explicit rate direction | `treasury/currency.py` | QA-001/002/003 | implemented |
| Route quotes: timestamp, TTL, source, data class; itemised all-in cost | `treasury/quotes.py`, `fixtures.py` | QA-001, QA-021 | implemented |
| Deadline intelligence: business days, holidays, cut-off, time zone, latest safe arrival | `treasury/calendar.py` | QA-023 | implemented |
| Liquidity timeline per currency, reserve, confirmed vs forecast | `treasury/liquidity.py` | QA-005, QA-006 | implemented (single source currency) |
| Deadline-constrained optimizer: 20 candidates, hard constraints first, INFEASIBLE with reasons, baseline comparison, decision record | `treasury/optimizer.py` | QA-004, QA-005, QA-007 | implemented |
| Tuition Shield: immutable beneficiary versions, field-level diff, trusted callback only, 48 h hold | `treasury/beneficiary.py`, `engine.parse_invoice` | QA-008, QA-009 | implemented |
| Policy engine POL-001…POL-023, ALLOW/ASK/DENY/RETURN_EXISTING, reason codes, rule IDs, policy version, fail closed | `treasury/policy.py` | QA-010/011/012, policy tests | implemented |
| Exact-action approval bound to an immutable fingerprint; token hash only; recheck on redemption | `treasury/intents.py`, `engine.py` | QA-013, QA-014, expiry test | implemented |
| Concurrency-safe cap reservations | `treasury/limits.py` | QA-015 (20 threads) | implemented (single process) |
| Kill switch (user only), idempotent executor, timeout and retry | `treasury/engine.py` | QA-016, QA-017, duplicate-invoice test | implemented |
| Ledger: H_n = SHA-256(canonical_json ‖ H_(n−1)), redaction, checkpoints, JSONL/CSV export, fail-closed on outage | `treasury/ledger.py` | QA-018/019/020/022 | implemented |
| Stress and counterfactual lab | `treasury/stress.py` | `evidence/stress.json` | implemented |
| 9 typed tools, max 6 calls per turn, user scope, tokens never returned to the model | `treasury/tools.py` | tool tests, QA-022 | implemented |
| Flagship 10-step demo and fixed-seed replay | `treasury/flagship.py`, `__main__.py` | QA-024, `evidence/` | implemented |
| Demo site page "Payment plan 2.0" and a persistent SIMULATION bar on every page | `demo/template.html` | screenshots at 1280 px and 400 px, no console errors | implemented (static, recorded outputs) |

The status of every QA case, policy rule and requirement is written into `docs/TuitionPilot_2_0_Backlog_with_Evidence.xlsx` (sheets QA_Test_Cases, Policy_Rules, Requirement_Map, Engine_Results, Roadmap).

## 3. Financial example (fixtures, assumptions visible)

Obligation: 45,000.00 CNY due 2026-09-03; university processing buffer 5 business days → latest safe arrival 2026-08-27. Planning time: Mon 2026-08-17 09:00 Asia/Almaty. Mid rate 0.01480 CNY per 1 KZT (fixture).

| Route | At mid | FX spread | Variable fee | Fixed + intermediary | Known all-in (KZT) | Arrival (bd) | Result |
|---|---|---|---|---|---|---|---|
| R1 Bank wire | 3,040,541 | 36,930 | 76,937 | 1,450 | 3,155,857 | 5–7 | feasible (baseline) |
| R2 Fintech | 3,040,541 | 18,353 | 61,178 | 1,030 | 3,121,102 | 2–4 | feasible |
| R3 Account | 3,040,541 | 12,211 | 45,791 | 620 | **3,099,163** | 1–3 | **selected** |
| R4 Cheap, slow | 3,040,541 | 9,149 | 30,497 | 350 | 3,080,537 | 7–10 | rejected: arrives 2026-08-31 |
| R5 Stale quote | 3,040,541 | 15,279 | 45,837 | 430 | 3,102,087 | 2–4 | rejected: quote 24 h old |

Twenty candidates (5 routes × 4 schedules); 9 pass every hard limit. Selected: R3, one transfer, 3,099,162.83 KZT. Versus the R1 baseline on the same amount and deadline: **56,694.12 KZT (1.80%) less, SIMULATED, not a real or guaranteed saving.** Receiving-bank charges are unknown and excluded.

Stress results (`evidence/stress.json`): FX shocks change only later tranches. A 10% weaker KZT makes the four-part R2 plan breach the 500,000 KZT reserve (workbook C5). A delay of up to 5 business days keeps R3 feasible with 0 days of buffer. Sponsor funds arriving 10 days late make every plan **INFEASIBLE** with reasons. An outage on R3 moves the choice to R2 (+21,939 KZT). Stale quotes or a changed beneficiary block every plan. Planning on 1 Aug instead makes R4 feasible and cheapest.

## 4. Policy, security and adversarial results

Command: `python -m pytest -q` → **74 passed** (39 v0.1 + 35 v2.0). JUnit XML: `evidence/pytest-junit.xml`.

* Critical false-allow outcomes in this suite: **0**. This is a test gate, not proof of zero real-world risk.
* Invoice prompt injection: denied (POL-018); caps, tier and kill switch unchanged; no execution.
* Changed bank details in a polite invoice: denied (POL-010); after switching to the new version: denied with a 48 h hold (POL-011); verifying from "invoice" as the contact source is rejected.
* Above the autonomous cap: ASK (POL-005, POL-009). Amount +0.01 KZT after approval: DENY (POL-021); the old approval becomes INVALIDATED.
* Replay of a used token: rejected. Expiry after 15 minutes: rejected.
* 20 parallel reservations against a 10,000,000 KZT cap: exactly 3 succeed.
* Timeout followed by two retries: 1 execution record, 1 simulated debit.
* Audit outage: DENY (POL-019); nothing reserved, nothing executed.
* Ledger: an edit in the middle is detected at the exact sequence; deleting the tail is detected only against an exported checkpoint (documented limitation).
* Raw tokens and full account numbers do not appear in the ledger or in tool outputs.
* Fixed-seed replay: identical output hash and ledger head.

## 5. Known limitations and TBDs

* In-memory state; locks stand in for database unique constraints and transactions. A PostgreSQL (or PocketBase after a spike) store is needed before any multi-user pilot.
* No authentication layer. `user_id` and session-user checks exist (POL-001), but there is no real identity provider.
* Fixture-only quotes and fees; quote TTL (30 min), holiday calendars, cut-offs and arrival-time ranges are TBD per provider and corridor.
* Single source currency in the liquidity book. Multi-currency wallets and converted totals need a disclosed base currency, rate and timestamp (not built yet).
* Tranche plans assume the university accepts part-payments (TBD). Later tranches are priced at today's quote as a labelled assumption.
* The LLM planner from v0.1 is not yet wired to the 2.0 tool router; the 2.0 tools are exercised by tests and by the deterministic flagship.
* The UI is a static site showing recorded outputs. There is no Next.js app (ADR-001), and the approval-comprehension usability test with students (QA-025) has not been run.
* Hash chain: tamper-evident, not legally non-repudiable; tail deletion needs external checkpoints.
* No regulatory status of any kind is claimed. Real money requires legal, compliance and partner review.

## 6. Run, test, seed and replay

```bash
python -m pip install -e ".[test]"
python -m pytest -q                                   # 74 tests
python -m tuitionpilot.treasury flagship --out evidence   # 10-step demo + evidence files
python -m tuitionpilot.treasury replay --seed 7          # identical hashes expected
python tools/build_demo.py                             # rebuild the demo site from engine runs
```
