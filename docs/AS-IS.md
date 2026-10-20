# AS-IS report: TuitionPilot repository before 2.0 work

Baseline commit `ec77771` (v0.1.0), audited 17 October 2026 against *TuitionPilot 2.0 Master Specification*, *Claude Master Brief* and the *Financial Model and Product Backlog* workbook.

## 1. Stack and structure (evidence)

| Item | Finding |
|---|---|
| Language / runtime | Python ≥ 3.10, standard library only (`pyproject.toml`, `dependencies = []`) |
| Package manager | pip / setuptools. No `package.json`, no Next.js, no TypeScript |
| Database / migrations | None. All state is in-memory Python objects |
| Environment config | None needed. No `.env`, no secrets, no network calls (the optional LLM planner reads API keys from env vars only if enabled) |
| Tests | `tests/test_tuitionpilot.py`, 39 tests, `python -m pytest -q` → **39 passed** |
| CI | `.github/workflows/ci.yml`: pytest on Python 3.10 and 3.12 plus the demo run (not yet executed on GitHub, since the repo has not been pushed) |
| UI | `demo/index.html`: a static single-file site (vanilla JS). It replays recorded engine runs and ports some logic to JS. There is no server. |
| Deployment | Published as a static artifact page. No server deployment. |

## 2. What exists and works (with evidence)

| Capability | Where | Evidence |
|---|---|---|
| Decimal money (`Decimal`, half-up to cents) | `models.py` | Used everywhere; no floats in money paths except the FX simulator's random walk (`market.py`) and the backtest statistics |
| Seeded FX simulator | `market.py` | Deterministic paths (seed) |
| 5 payment rails, quoting, risk-adjusted ranking, deadline/limit/reference eligibility | `rails.py` | `test_ranking_prefers_cheap_eligible_routes`, `test_route_after_deadline_is_ineligible` |
| Tranche FX planner with hard dates and an opportunistic trigger | `fxplan.py` | `test_plan_always_meets_deadline_across_paths` |
| Policy engine: tiers L0–L3, per-payment/daily/30-day caps, FX concentration, new-payee cool-off, kill switch, no self-escalation | `policy.py` | 11 policy tests |
| Step-up code: one-time, 15-minute TTL, bound to an action fingerprint, policy re-check on redemption | `policy.py`, `agent.py` | `test_confirmation_token_is_one_time_and_expires`, scenario S3 |
| Guards: prompt-injection patterns, payee account mismatch, urgency, amount anomaly | `guards.py` | `test_injection_detection`, S2 |
| SHA-256 hash-chained audit log | `audit.py` | `test_audit_chain_detects_tampering`, `..._deletion` |
| Idempotent conversions and transfers | `bank.py` | `test_idempotent_payment_never_double_pays` |
| Tool allow-list, LLM planner gated by policy | `planner.py`, `agent.py` | `test_llm_tool_calls_are_allowlisted`, `test_llm_planner_with_fake_transport_still_policy_gated` |
| Monte-Carlo backtest | `backtest.py` | `test_backtest_routing_saves_on_average` |

## 3. What is partial, mock or wrong compared with the 2.0 spec

| Area | Gap | Severity |
|---|---|---|
| Money representation | `Decimal` rounded to 2 dp for every currency. No per-currency minor-unit metadata (JPY/KRW/VND have 0 decimals). No explicit quote direction on rates. | High |
| Fee model | Rails mix a USD fixed fee, a % fee and an FX markup. There is no intermediary fee, no split between known and estimated costs, and no quote timestamp, TTL, source or data-quality label. | High |
| Deadlines | Calendar days only. No business days, cut-off times, time zones, university processing buffer or "latest safe arrival" separate from the due date. | High |
| Optimizer | Picks between "staged FX" and "best direct". The risk-adjusted ranking uses a **hidden reliability penalty** (spec: no hidden score). No route × schedule × date candidates, no explicit INFEASIBLE, no rejected-alternative record. | Critical |
| Liquidity | A budget runway exists, but no `liquidity(t)` timeline per currency, no minimum reserve and no confirmed vs forecast inflows. | High |
| Beneficiary | A payee is a mutable object with a `verified` flag. No immutable versions, no field-level diff and no record of verification method or trusted contact source. | Critical |
| Approval binding | The fingerprint covers type, value, payee id, rail and params. It does **not** cover beneficiary version, currency pair, quote id or policy version. The raw code is stored in memory, and the 6-character code appears in audit entries and notifications. | Critical |
| Concurrency | Caps are checked, then spend is recorded. There is no atomic reservation, so parallel requests could both pass. | Critical |
| Audit | The chain covers modification and deletion in the middle. There are no external checkpoints (tail deletion is undetected), no redaction, no event IDs, no policy/app version and no environment tag. The engine **does not fail closed** if an audit write fails. | High |
| Policy output | `allow / require_confirmation / deny` with free-text rules. Rule IDs do not match the workbook's POL-001…022. No identity check, no policy version, no quote freshness or liquidity rule, no fail-closed on unknown state. | High |
| Stress testing | None, apart from the Monte-Carlo backtest. | High |
| Replay | Seeds exist, but there is no ScenarioRun record with input/output hashes and versions. | Medium |
| Tools | 9 tools exist but are not the 9 named in the spec; no per-tool user-scope check. | Medium |
| UI | Static site. No persistent "SIMULATION — NO REAL MONEY MOVES" status on every screen. | High |
| Claims | Site hero and deck cover say "Never overpay. Never get scammed." This reads as guaranteed savings and fraud elimination, which the spec forbids. "A log nobody can quietly edit" overstates the hash chain. | High |

## 4. Missing

Identity/user scope, persistent storage with unique constraints, the ApprovalChallenge / PaymentIntent / ExecutionAttempt state machine, CSV export, a currency-integrity rule, an audit-outage path, privacy redaction, weekend and cut-off logic, the Stress/Counterfactual Lab and a Tuition Shield UI.

## 5. Blocked (needs decisions or external input)

| Item | Why blocked |
|---|---|
| Real quotes, partner APIs, KYC/AML, live execution | Out of MVP scope by spec; needs legal/compliance and partner approval |
| Provider-specific quote TTL, real holiday calendars, arrival-time model | No sourced data; fixtures are used and labelled TBD |
| Database choice (PocketBase vs PostgreSQL) | Needs a technical spike; the 2.0 core uses an in-memory store with lock-protected unique keys and reservations, documented as a prototype boundary |
| Next.js/TypeScript stack | See ADR-001 |

## 6. Plan (preserves working code)

1. Keep v0.1 modules and tests unchanged; the existing demo depends on them.
2. Add a separate package `tuitionpilot/treasury/` for the 2.0 core: minor-unit money, quotes, calendar, beneficiary versions, limit reservations, policy POL-001…022, approval binding, intents and executor, ledger with checkpoints, optimizer, stress lab, the 9 spec tools, flagship demo and replay.
3. Map the workbook QA-001…026 to tests named `test_qa0NN_*`, run them, and write the results back to the workbook copy only after running.
4. Add a 2.0 page to the demo site, rendered from a recorded flagship run, with a persistent simulation badge; correct the forbidden claims in the site and deck.
