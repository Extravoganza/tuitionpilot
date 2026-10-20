# Gap matrix: Master spec v2.1 (Country Intelligence, China-first) vs. repository

Date: 19 Oct 2026. Status is set only from code that exists and tests that were actually run
(`python -m pytest -q`: 103 passed; `python -m tuitionpilot.treasury china`: suite cil-mvp-1.0, 16/16 PASS, release gate PASS).

> SIMULATION — NO REAL MONEY MOVES. All China-pack profiles are DEMO FIXTURES (synthetic universities and rules).

| Spec § | Requirement | Status | Code path | Tests / evidence | Next action |
|---|---|---|---|---|---|
| 5 | Versioned country/corridor/university profiles, immutable after use | Done (in-memory) | `treasury/country.py` `ProfileRegistry` | `test_used_profile_version_is_frozen_and_update_creates_v2` | Persist in a DB with unique (profile_id, version) |
| 5 | Sources with scope, checked_at, quality; no invented dates | Done | `SourceRecord`, `RESEARCH_SOURCES` | `test_research_sources_are_not_verified_and_have_no_invented_dates` | Verify real university pages, set checked_at |
| 5 | Stale critical rule → RULES_OUTDATED, no auto-allow | Done | `check_eligibility` | CIL-003, `test_cil003_*` | Freshness thresholds per source type (ADR) |
| 5 | Source scope mismatch | Done | `_scope_ok`, `check_method` | CIL-004 | |
| 5 | Output contract (status, methods, documents, sources, trace, quality, version) | Done | `check_eligibility` return value | `evidence/china_corridors.json` | |
| 6 | Two separate China corridors; no mirroring of inbound methods | Done | `inbound_profile`, `outbound_profile`, `resolve_corridor` | CIL-005, `test_inbound_method_applied_to_outbound_is_rejected` | |
| 6 | Demo moment: same invoice in two contexts | Done | `china_demo.run`, site page "China corridors" | screenshots, `evidence/china_corridors.json` | |
| 6 | Real university profiles | Not done (by design) | `RESEARCH_SOURCES` listed as NOT_VERIFIED | | Check pages, then publish profiles as v1 VERIFIED_SOURCE |
| 7 | Readiness statuses READY / MISSING_DOCUMENTS / MANUAL_REVIEW / RULES_OUTDATED / NOT_SUPPORTED | Done | `check_eligibility` | `test_missing_document_blocks_and_adding_it_makes_ready` | |
| 7 | Invoice extraction with confidence and source span | Partial | `engine.parse_invoice` (fields, injection guard) | QA-009 | Add confidence + source_span per field |
| 8 | Eligibility predicates; UNKNOWN never eligible | Done | `check_method` | `test_unknown_method_is_never_eligible` | |
| 9 | Deadline intelligence: timezone, holidays, buffer, ranges, unknown stays unknown | Done | `project_deadline` | `test_deadline_projection_known_and_unknown`, `test_holiday_in_demo_calendar_moves_safe_date` | Load official calendars |
| 10 | Survival simulator per currency | Partial | `treasury/liquidity.py` (KZT) | QA-006 | Multi-currency horizons 30/60/90/180 |
| 11 | Optimizer hard constraints first, INFEASIBLE | Done (2.0) | `treasury/optimizer.py` | QA-004, QA-005, PAY-001 | Feed only CIL-eligible methods (mapping exists: `PaymentMethod.route_id`) |
| 12 | Tuition Shield 2.0, approval invalidation | Done (2.0) | `beneficiary.py`, `intents.py` | QA-008, QA-014, SHIELD-001 | |
| 13 | Lifecycle statuses, submitted ≠ credited, reconciliation, manual resolution | Done | `treasury/tracking.py` | REC-001, `test_exact_credit_reconciles_and_fuzzy_goes_to_review` | |
| 14 | Counterfactual engine | Done (2.0) | `treasury/stress.py` | `evidence/stress.json` | Country-rule-expiry scenario |
| 15 | Evaluation Lab, INFRA_ERROR ≠ PASS, release gate | Done | `treasury/evaluation.py` | EVAL-001, `evidence/evaluation_run.json` | Latency metric only when measured |
| 16 | Evidence Pack, verify, redaction, token export blocked | Done (JSON) | `treasury/evidence.py` | EVD-001, `test_export_with_approval_token_is_blocked` | PDF rendering of the pack |
| 17 | 9 tools, max 6 calls | Done (2.0) | `treasury/tools.py` | POL-001 | Add CIL tools (get_country_profile, check_method_eligibility) to the router |
| 18 | Policy L0–L3, caps, approval TTL, kill switch, fail closed | Done (2.0) | `policy.py`, `limits.py` | QA-010…QA-016, AUD-001 | |
| 21 | REST API `/api/v1/...` | Not done | | | Thin HTTP layer over the same functions |
| 20 | PostgreSQL vs PocketBase | Open | ADR-001 keeps in-process stores | | ADR after a transactions/locks spike |
| 22 | UI screens and badges | Done for demo | `demo/template.html` pages Plan, China corridors, Results | Playwright screenshots, 0 JS errors, no overflow at 400 px | |
| 23 | Privacy: no passport/card data, redaction | Done | `ledger.redact`, `evidence.build_pack` | QA-022 | Retention policy before a real pilot |
