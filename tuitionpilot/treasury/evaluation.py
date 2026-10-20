"""Evaluation Lab (spec v2.1 §15, Appendix C).

Each case runs real engine code and reports PASS, FAIL or INFRA_ERROR. An
exception, a timeout or a missing fixture is INFRA_ERROR, never PASS. The
release gate passes only when every critical case actually executed and
passed and the critical false-allow count is zero. The gate applies to this
named suite only; it is not proof of absolute security, and it never deploys
anything.
"""
from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Callable

from . import country as C
from . import fixtures as F
from .currency import Amount
from .engine import TreasuryEngine
from .evidence import build_pack, verify_pack
from .limits import Caps, LimitBook
from .tracking import Expected, IdempotencyConflict, PaymentTracker, SandboxProvider
from .tools import ToolRouter

SUITE_VERSION = "cil-mvp-1.0"
OB = "OB-TUITION-AUT26"
USER = "student-001"


@dataclass
class Case:
    case_id: str
    setup: str
    expected: str
    severity: str                        # Critical | High
    fn: Callable[[], tuple]              # returns (passed: bool, observed: str, false_allow: bool)


def _req(ctx="A", **over):
    c = C.DEMO_CONTEXTS[ctx]
    base = dict(corridor_id=C.FROM_ABROAD_TO_CHINA if ctx == "A" else C.FROM_CHINA_TO_ABROAD,
                university_id=c["university_id"], sender_country=c["sender_country"], payer_type=c["payer_type"],
                invoice_currency=c["invoice_currency"], reference=c["reference"], documents=c["documents"],
                instruments=c["instruments"])
    base.update(over)
    return C.EligibilityRequest(**base)


def cil_001():
    r = C.check_eligibility(C.inbound_profile(), _req())
    ok = r["eligibility_status"] == "READY" and r["eligible_methods"] == ["A-WIRE"]
    return ok, f"{r['eligibility_status']}, eligible {r['eligible_methods']}", False


def cil_002():
    r = C.resolve_corridor("KZ", "DE", "STUDENT")
    allowed = r["corridor_id"] is not None
    return r["status"] == "NOT_SUPPORTED" and not allowed, f"{r['status']} {r['reason_codes']}", allowed


def cil_003():
    p = C.inbound_profile()
    rules = tuple(replace(x, valid_until=date(2026, 7, 31)) if x.rule_id == "A-REF-01" else x for x in p.rules)
    r = C.check_eligibility(replace(p, rules=rules), _req())
    allowed = bool(r["eligible_methods"])
    return r["eligibility_status"] == "RULES_OUTDATED" and not allowed, \
        f"{r['eligibility_status']}, eligible {r['eligible_methods']}", allowed


def cil_004():
    r = C.check_eligibility(C.inbound_profile(), _req())
    m = next(x for x in r["methods"] if x["method_id"] == "A-TSINGHUA")
    allowed = m["status"] == "ELIGIBLE"
    return "SOURCE_SCOPE_MISMATCH" in m["reason_codes"] and not allowed, f"{m['status']} {m['reason_codes']}", allowed


def cil_005():
    a = C.run_context("A")
    b = C.run_context("B")
    differ = (a["corridor"]["corridor_id"] != b["corridor"]["corridor_id"]
              and a["profile"]["profile_id"] != b["profile"]["profile_id"]
              and not {m["method_id"] for m in a["eligibility"]["methods"]} & {m["method_id"] for m in b["eligibility"]["methods"]})
    return differ, f"A={a['profile']['profile_id']} B={b['profile']['profile_id']}, no shared methods", False


def pay_001():
    d = TreasuryEngine().plan(OB, log=False)
    r4 = [c for c in d.candidates if c.route_id == "R4"]
    late = all(not c.feasible for c in r4)
    return late, "R4 (cheapest) rejected on deadline in all schedules", not late


def shield_001():
    e = TreasuryEngine()
    d = e.plan(OB)
    spec = e.intent_from_plan(OB, d)
    _, token = e.request_approval(spec, USER)
    inv = e.parse_invoice(F.CHANGED_INVOICE_CLEAN)
    new_v = e.registry.history("BEN-SDU")[-1]
    changed = replace(spec, beneficiary_version_id=new_v.version_id)
    r = e.execute(changed, USER, approval_token=token)
    allowed = r.get("state") == "SIMULATED_SUBMITTED"
    return r["state"] == "NOT_EXECUTED" and not allowed, f"{r['state']} {r.get('matched_rule_ids')}", allowed


def pol_001():
    e = TreasuryEngine()
    caps_before = (e.caps.autonomous_minor, e.caps.per_payment_minor)
    res = ToolRouter(e, USER, {}).run_turn([{"tool": "raise_autonomous_cap", "args": {"new_cap": "99999999"}}])[0]
    unchanged = (e.caps.autonomous_minor, e.caps.per_payment_minor) == caps_before
    blocked = res["status"] == "rejected" and unchanged
    return blocked, f"{res['status']}: {res.get('error', '')}", not blocked


def pol_002():
    e = TreasuryEngine()
    d = e.plan(OB)
    spec = e.intent_from_plan(OB, d)
    _, token = e.request_approval(spec, USER)
    e.execute(spec, USER, approval_token=token)
    st, _ = e.approvals.redeem(token, spec, e.clock.now())
    return st == "INVALID:REUSED", st, st == "VALID"


def pol_003():
    caps = Caps(daily_minor=1_000_000_00, per_payment_minor=1_000_000_00)
    book, wins, barrier = LimitBook(caps), [], threading.Barrier(20)

    def w(i):
        barrier.wait()
        r, _ = book.try_reserve(f"I{i}", Amount.of("300000", "KZT"), F.NOW)
        if r:
            wins.append(r)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    over = book.committed_total() > caps.daily_minor
    return len(wins) == 3 and not over, f"{len(wins)} of 20 reservations granted", over


def aud_001():
    e = TreasuryEngine()
    d = e.plan(OB)
    spec = e.intent_from_plan(OB, d)
    _, token = e.request_approval(spec, USER)
    e.ledger.available = False
    r = e.execute(spec, USER, approval_token=token)
    allowed = bool(e.executions())
    return r["decision"] == "DENY" and not allowed, f"{r['decision']} {r['reason_codes']}", allowed


def idem_001():
    p = SandboxProvider()
    a = p.submit("K1", {"amount": 1})
    b = p.submit("K1", {"amount": 1})
    return p.executions == 1 and b["replayed"], f"{p.executions} execution, replay returned {b['provider_ref']}", p.executions > 1


def idem_002():
    p = SandboxProvider()
    p.submit("K1", {"amount": 1})
    try:
        p.submit("K1", {"amount": 2})
    except IdempotencyConflict as ex:
        return p.executions == 1, f"{ex.code}", False
    return False, "second payload accepted", True


def rec_001():
    e = TreasuryEngine()
    t = PaymentTracker(e.ledger)
    now = e.clock.now()
    pay = t.create("PI-X", Expected(Amount.of("45000", "CNY"), "BV-1", "REF", (date(2026, 8, 18), date(2026, 8, 20))), now)
    for s in ("READY_FOR_APPROVAL", "APPROVED", "SUBMITTED_SIMULATED", "PROCESSING_SIMULATED", "SENT_SIMULATED"):
        t.advance(pay.payment_id, s, now)
    r = t.reconcile(pay.payment_id, [], now)
    settled = t.obligation_settled(pay)
    return pay.status == "CREDIT_PENDING" and not settled, f"{pay.status}, settled={settled}", settled


def evd_001():
    e = TreasuryEngine()
    pack = build_pack({"plan": {"total": "3,099,162.83 KZT"}, "eligibility": {"status": "READY"}}, e.ledger, e.clock.now())
    pack["items"]["plan"]["total"] = "1.00 KZT"
    v = verify_pack(pack)
    return v["status"] == "INVALID", f"{v['status']} {[p['code'] for p in v['problems']]}", v["status"] == "VALID"


def _sleeper():
    time.sleep(5)
    return True, "should not be reached", False


def eval_001():
    r = run_case(Case("EVAL-001-inner", "", "", "High", _sleeper), timeout_s=0.2)
    return r["result"] == "INFRA_ERROR", f"inner case → {r['result']}", r["result"] == "PASS"


CASES = [
    Case("CIL-001", "Supported corridor, fresh profile", "Eligibility ready subject to remaining gates", "High", cil_001),
    Case("CIL-002", "Unknown corridor", "NOT_SUPPORTED; no guessed method", "Critical", cil_002),
    Case("CIL-003", "Stale critical source", "RULES_OUTDATED; no allow", "Critical", cil_003),
    Case("CIL-004", "Source applies to another university", "SOURCE_SCOPE_MISMATCH", "Critical", cil_004),
    Case("CIL-005", "Reverse corridor", "Different profile and rules loaded", "Critical", cil_005),
    Case("PAY-001", "Cheapest route misses deadline", "INFEASIBLE_DEADLINE", "High", pay_001),
    Case("SHIELD-001", "Beneficiary changes after approval", "Token invalidated; no execution", "Critical", shield_001),
    Case("POL-001", "LLM asks to raise cap", "Policy unchanged; denied", "Critical", pol_001),
    Case("POL-002", "Approval token replay", "Rejected", "Critical", pol_002),
    Case("POL-003", "Concurrent intents exceed cap", "Atomic cap enforcement", "Critical", pol_003),
    Case("AUD-001", "Audit unavailable", "Execution blocked", "Critical", aud_001),
    Case("IDEM-001", "Same key and payload replay", "No duplicate execution", "Critical", idem_001),
    Case("IDEM-002", "Same key, changed payload", "IDEMPOTENCY_CONFLICT", "Critical", idem_002),
    Case("REC-001", "Submitted but no credit event", "CREDIT_PENDING, not CREDITED", "High", rec_001),
    Case("EVD-001", "Evidence item tampered", "Integrity INVALID", "High", evd_001),
    Case("EVAL-001", "Test times out", "INFRA_ERROR, not PASS", "High", eval_001),
]


def run_case(case: Case, timeout_s: float = 10.0) -> dict:
    box: dict = {}

    def target():
        try:
            box["out"] = case.fn()
        except Exception as ex:                    # noqa: BLE001 — any crash is an infra error, never a pass
            box["err"] = f"{type(ex).__name__}: {ex}"
    t0 = time.perf_counter()
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout_s)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    base = {"case_id": case.case_id, "setup": case.setup, "expected": case.expected, "severity": case.severity,
            "duration_ms": ms}
    if th.is_alive():
        return {**base, "result": "INFRA_ERROR", "observed": f"timeout after {timeout_s}s", "false_allow": False}
    if "err" in box:
        return {**base, "result": "INFRA_ERROR", "observed": box["err"], "false_allow": False}
    passed, observed, false_allow = box["out"]
    return {**base, "result": "PASS" if passed else "FAIL", "observed": observed, "false_allow": bool(false_allow)}


def run_suite(seed: int = 7, timeout_s: float = 10.0, now=None) -> dict:
    results = [run_case(c, timeout_s) for c in CASES]
    return {"suite_version": SUITE_VERSION, "case_count": len(results), "seed": seed,
            "environment": f"SIMULATION, Python {platform.python_version()}",
            "timestamp": (now or F.NOW).isoformat(), "engine_versions": {"policy": "2.0.0", "cil": "1.0"},
            "results": results, "summary": summarize(results), "release_gate": release_gate(results)}


def summarize(results: list[dict]) -> dict:
    s = {"PASS": 0, "FAIL": 0, "INFRA_ERROR": 0}
    for r in results:
        s[r["result"]] += 1
    s["critical_false_allow"] = sum(1 for r in results if r["severity"] == "Critical" and r["false_allow"])
    return s


def release_gate(results: list[dict]) -> dict:
    crit = [r for r in results if r["severity"] == "Critical"]
    reasons = []
    if any(r["false_allow"] for r in crit):
        reasons.append("critical false-allow present")
    if any(r["result"] != "PASS" for r in crit):
        reasons.append("a critical case did not pass")
    if any(r["result"] == "INFRA_ERROR" for r in results):
        reasons.append("infra errors are not passes")
    return {"status": "PASS" if not reasons else "BLOCKED", "reasons": reasons,
            "scope": f"suite {SUITE_VERSION} only; not proof of absolute security; no automatic deploy"}
