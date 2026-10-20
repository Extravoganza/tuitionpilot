"""Spec v2.1: Country Intelligence Layer, China-first pack, tracking, evidence pack, evaluation lab."""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tuitionpilot.treasury import country as C
from tuitionpilot.treasury.china_demo import lifecycle, profile_versioning, run
from tuitionpilot.treasury.currency import Amount
from tuitionpilot.treasury.engine import TreasuryEngine
from tuitionpilot.treasury.evaluation import CASES, Case, release_gate, run_case, run_suite
from tuitionpilot.treasury.evidence import ExportBlocked, build_pack, verify_pack
from tuitionpilot.treasury.tracking import (Expected, IdempotencyConflict, IllegalTransition, PaymentTracker,
                                            SandboxProvider)

AS_OF = date(2026, 8, 17)


def req(ctx="A", **over):
    c = C.DEMO_CONTEXTS[ctx]
    base = dict(corridor_id=C.FROM_ABROAD_TO_CHINA if ctx == "A" else C.FROM_CHINA_TO_ABROAD,
                university_id=c["university_id"], sender_country=c["sender_country"], payer_type=c["payer_type"],
                invoice_currency=c["invoice_currency"], reference=c["reference"], documents=c["documents"],
                instruments=c["instruments"], as_of=AS_OF)
    base.update(over)
    return C.EligibilityRequest(**base)


# ------------------------------------------------------------- corridor resolver
def test_corridor_resolver_is_directional():
    assert C.resolve_corridor("KZ", "CN", "STUDENT")["corridor_id"] == C.FROM_ABROAD_TO_CHINA
    assert C.resolve_corridor("CN", "KZ", "PARENT_SPONSOR")["corridor_id"] == C.FROM_CHINA_TO_ABROAD


def test_cil002_unknown_corridor_is_not_supported_and_has_no_route():
    r = C.resolve_corridor("KZ", "DE", "STUDENT")
    assert r["status"] == "NOT_SUPPORTED" and r["corridor_id"] is None and "CORRIDOR_UNSUPPORTED" in r["reason_codes"]
    assert C.resolve_corridor("CN", "CN", "STUDENT")["status"] == "NOT_SUPPORTED"


def test_ambiguous_corridor_asks_a_question():
    r = C.resolve_corridor("", "CN", "STUDENT")
    assert r["status"] == "NEEDS_INPUT" and "CORRIDOR_AMBIGUOUS" in r["reason_codes"] and "sender_country" in r["question"]


# ------------------------------------------------------------- eligibility
def test_cil001_supported_corridor_fresh_profile_is_ready():
    r = C.check_eligibility(C.inbound_profile(), req())
    assert r["eligibility_status"] == "READY" and r["eligible_methods"] == ["A-WIRE"]
    assert r["profile_version"] == 1 and r["sources"] and r["decision_trace"]


def test_unknown_method_is_never_eligible():
    r = C.check_eligibility(C.inbound_profile(), req())
    m = {x["method_id"]: x for x in r["methods"]}
    assert m["A-EDU"]["status"] == "UNKNOWN" and "METHOD_UNKNOWN" in m["A-EDU"]["reason_codes"]
    assert "A-EDU" not in r["eligible_methods"]


def test_stale_method_status_is_stale_not_eligible():
    m = {x["method_id"]: x for x in C.check_eligibility(C.inbound_profile(), req())["methods"]}
    assert m["A-CARD"]["status"] == "STALE" and "RULES_OUTDATED" in m["A-CARD"]["reason_codes"]


def test_instrument_requirement_excludes_wallet_for_payer_abroad():
    m = {x["method_id"]: x for x in C.check_eligibility(C.inbound_profile(), req())["methods"]}
    assert m["A-WALLET"]["status"] == "INELIGIBLE"
    with_acct = C.check_eligibility(C.inbound_profile(), req(instruments=frozenset({"mainland_bank_account"})))
    assert "A-WALLET" in with_acct["eligible_methods"]


def test_cil003_stale_critical_rule_blocks_everything():
    p = C.inbound_profile()
    rules = tuple(replace(r, valid_until=date(2026, 7, 31)) if r.rule_id == "A-REF-01" else r for r in p.rules)
    r = C.check_eligibility(replace(p, rules=rules), req())
    assert r["eligibility_status"] == "RULES_OUTDATED" and r["eligible_methods"] == []


def test_cil004_source_for_another_university_does_not_count():
    m = {x["method_id"]: x for x in C.check_eligibility(C.inbound_profile(), req())["methods"]}
    assert m["A-TSINGHUA"]["status"] == "UNKNOWN" and "SOURCE_SCOPE_MISMATCH" in m["A-TSINGHUA"]["reason_codes"]


def test_cil005_reverse_corridor_loads_different_profile_rules_and_methods():
    a, b = C.run_context("A"), C.run_context("B")
    assert a["corridor"]["corridor_id"] != b["corridor"]["corridor_id"]
    assert a["profile"]["profile_id"] != b["profile"]["profile_id"]
    assert not {m["method_id"] for m in a["eligibility"]["methods"]} & {m["method_id"] for m in b["eligibility"]["methods"]}
    assert {r["rule_id"] for r in a["eligibility"]["rules"]}.isdisjoint({r["rule_id"] for r in b["eligibility"]["rules"]})
    assert a["calendar"]["tz"] != b["calendar"]["tz"]


def test_inbound_method_applied_to_outbound_is_rejected():
    a_wire = C.inbound_profile().methods[0]
    p = C.outbound_profile()
    r = C.check_method(a_wire, p, req("B"), True)
    assert r["status"] == "INELIGIBLE" and "SOURCE_SCOPE_MISMATCH" in r["reason_codes"]


def test_missing_document_blocks_and_adding_it_makes_ready():
    b = C.run_context("B")
    assert b["eligibility"]["eligibility_status"] == "MISSING_DOCUMENTS" and b["eligibility"]["eligible_methods"] == []
    fixed = C.run_context("B", extra_documents=frozenset({"relationship_proof"}))
    assert fixed["eligibility"]["eligibility_status"] == "READY" and fixed["eligibility"]["eligible_methods"] == ["B-WIRE"]


def test_relationship_proof_only_required_for_sponsor_payer():
    r = C.check_eligibility(C.outbound_profile(), req("B", payer_type="STUDENT"))
    assert "relationship_proof" not in [d["document"] for d in r["required_documents"]]


def test_unknown_reference_support_needs_manual_review():
    fixed = C.run_context("B", extra_documents=frozenset({"relationship_proof"}))
    m = {x["method_id"]: x for x in fixed["eligibility"]["methods"]}
    assert m["B-CARD"]["status"] == "MANUAL_REVIEW"


def test_research_sources_are_not_verified_and_have_no_invented_dates():
    for s in C.RESEARCH_SOURCES:
        assert s.status == "NOT_VERIFIED" and s.published_at is None and s.checked_at is None


# ------------------------------------------------------------- versions / deadlines
def test_used_profile_version_is_frozen_and_update_creates_v2():
    v = profile_versioning()
    assert "publish v2" in v["edit_after_use"] and v["versions"] == [1, 2]
    assert v["v1_buffer_bd"] == 5 and v["v2_buffer_bd"] == 4


def test_deadline_projection_known_and_unknown():
    p = C.inbound_profile()
    d = C.project_deadline(p, p.methods[0], date(2026, 9, 3), AS_OF)
    assert d["latest_safe_arrival"] == "2026-08-27" and d["status"] == "ON_TIME"
    assert d["arrival_range"] == ["2026-08-18", "2026-08-20"]
    no_time = C.project_deadline(p, replace(p.methods[0], processing_bd=None), date(2026, 9, 3), AS_OF)
    assert no_time["status"] == "NEEDS_INPUT" and no_time["latest_initiation"] is None
    no_cal = C.project_deadline(replace(p, calendar=C.CalendarProfile(None, supported=False)), p.methods[0],
                                date(2026, 9, 3), AS_OF)
    assert no_cal["latest_safe_arrival"] is None and no_cal["calendar_supported"] is False


def test_slow_method_misses_deadline():
    p = C.inbound_profile()
    d = C.project_deadline(p, replace(p.methods[0], processing_bd=(5, 10)), date(2026, 9, 3), AS_OF)
    assert d["status"] == "INFEASIBLE_DEADLINE"


def test_holiday_in_demo_calendar_moves_safe_date():
    p = C.inbound_profile()
    d = C.project_deadline(p, p.methods[0], date(2026, 10, 9), date(2026, 9, 20))
    assert d["latest_safe_arrival"] == "2026-09-25"     # Oct 1–7 are demo holidays


# ------------------------------------------------------------- tracking
def _tracker():
    e = TreasuryEngine()
    t = PaymentTracker(e.ledger)
    p = t.create("PI-1", Expected(Amount.of("45000", "CNY"), "BV-1", "REF-1", (date(2026, 8, 18), date(2026, 8, 20))),
                 e.clock.now())
    return e, t, p


def test_rec001_submitted_is_not_credited():
    e, t, p = _tracker()
    for s in ("READY_FOR_APPROVAL", "APPROVED", "SUBMITTED_SIMULATED", "PROCESSING_SIMULATED", "SENT_SIMULATED"):
        t.advance(p.payment_id, s, e.clock.now())
    assert not t.obligation_settled(p)
    assert t.reconcile(p.payment_id, [], e.clock.now())["status"] == "CREDIT_PENDING"
    assert p.status == "CREDIT_PENDING" and not t.obligation_settled(p)


def test_exact_credit_reconciles_and_fuzzy_goes_to_review():
    e, t, p = _tracker()
    for s in ("READY_FOR_APPROVAL", "APPROVED", "SUBMITTED_SIMULATED", "PROCESSING_SIMULATED", "SENT_SIMULATED"):
        t.advance(p.payment_id, s, e.clock.now())
    fuzzy = t.reconcile(p.payment_id, [{"amount": Amount.of("45000", "CNY"), "beneficiary_version_id": "BV-1",
                                        "reference": "REF-l", "date": date(2026, 8, 19)}], e.clock.now())
    assert fuzzy["status"] == "RECONCILIATION_REQUIRED" and "REFERENCE_MISMATCH" in fuzzy["reason_codes"]
    assert not t.obligation_settled(p)
    with pytest.raises(ValueError):
        t.resolve_manually(p.payment_id, "ops-1", "", "", e.clock.now())
    t.resolve_manually(p.payment_id, "ops-1", "bank confirmed typo in reference", "call log #12", e.clock.now())
    assert t.obligation_settled(p) and p.reconciliation["resolved_by"] == "ops-1"


def test_illegal_status_jump_is_refused():
    e, t, p = _tracker()
    with pytest.raises(IllegalTransition):
        t.advance(p.payment_id, "CREDITED_SIMULATED", e.clock.now())


def test_full_lifecycle_on_flagship_payment():
    life = lifecycle(TreasuryEngine())
    statuses = [x["status"] for x in life["payment"]["timeline"]]
    assert statuses.index("SUBMITTED_SIMULATED") < statuses.index("CREDIT_PENDING") < statuses.index("CREDITED_SIMULATED")
    assert life["payment"]["settled_before_credit"] is False and life["payment"]["settled"] is True
    assert all(x["label"] == "SIMULATION — NO REAL MONEY MOVES" for x in life["payment"]["timeline"])


def test_idem001_and_idem002_provider_idempotency():
    p = SandboxProvider()
    a = p.submit("K", {"amount": 100})
    b = p.submit("K", {"amount": 100})
    assert a["provider_ref"] == b["provider_ref"] and b["replayed"] and p.executions == 1
    with pytest.raises(IdempotencyConflict):
        p.submit("K", {"amount": 101})
    assert p.executions == 1


# ------------------------------------------------------------- evidence pack
def test_evidence_pack_verifies_and_detects_tamper_and_omission():
    e = TreasuryEngine()
    pack = build_pack({"a": {"x": 1}, "b": {"account": "CN62 0000 1111 2222 3333"}}, e.ledger, e.clock.now())
    assert verify_pack(pack)["status"] == "VALID"
    assert pack["items"]["b"]["account"] == "•••• 3333"               # PII redacted by default
    assert pack["manifest"]["limitations"]
    pack["items"]["a"]["x"] = 2
    assert verify_pack(pack)["status"] == "INVALID"
    pack2 = build_pack({"a": {"x": 1}, "b": {"y": 2}}, e.ledger, e.clock.now())
    del pack2["items"]["b"]
    assert verify_pack(pack2)["status"] == "INCOMPLETE"


def test_export_with_approval_token_is_blocked():
    e = TreasuryEngine()
    with pytest.raises(ExportBlocked):
        build_pack({"approval": {"approval_token": "abc123"}}, e.ledger, e.clock.now())


# ------------------------------------------------------------- evaluation lab
def test_eval001_timeout_and_crash_are_infra_error_not_pass():
    import time
    slow = run_case(Case("X", "", "", "High", lambda: (time.sleep(2), (True, "", False))[1]), timeout_s=0.1)
    assert slow["result"] == "INFRA_ERROR"

    def boom():
        raise KeyError("missing fixture")
    assert run_case(Case("Y", "", "", "High", boom))["result"] == "INFRA_ERROR"
    gate = release_gate([{"severity": "High", "result": "INFRA_ERROR", "false_allow": False}])
    assert gate["status"] == "BLOCKED"


def test_full_suite_passes_release_gate_with_zero_false_allow():
    s = run_suite()
    assert s["case_count"] == len(CASES) == 16
    assert s["summary"]["critical_false_allow"] == 0 and s["summary"]["INFRA_ERROR"] == 0
    assert s["release_gate"]["status"] == "PASS" and "not proof" in s["release_gate"]["scope"]


def test_china_demo_runs_end_to_end():
    r = run()
    assert r["contexts"]["A"]["eligibility"]["eligibility_status"] == "READY"
    assert r["contexts"]["B"]["eligibility"]["eligibility_status"] == "MISSING_DOCUMENTS"
    assert r["evidence_pack"]["verify"]["status"] == "VALID"
    assert r["evidence_pack"]["tampered_verify"]["status"] == "INVALID"


def test_planner_api_reproduces_flagship_plan():
    from tuitionpilot.treasury.planner_api import plan_for
    r = plan_for("45000", "2026-09-03", "2026-08-17", "4400000", "500000",
                 [("2026-08-18", "600000", "confirmed_inflow"), ("2026-08-24", "1200000", "committed_outflow"),
                  ("2026-08-31", "300000", "forecast_inflow")])
    assert r["status"] == "FEASIBLE" and r["selected"] == "C09" and r["savings"] == "56,694.12 KZT"
    late = plan_for("45000", "2026-09-03", "2026-08-17", "3600000", "500000",
                    [("2026-08-28", "600000", "confirmed_inflow"), ("2026-08-24", "1200000", "committed_outflow")])
    assert late["status"] == "INFEASIBLE"
