"""China-first demo (spec v2.1 §6, §13, §16, §15, §27): one deterministic run that the
site and the evidence folder both use.

1. Same conditional invoice, two corridors (A inbound, B outbound).
2. B is blocked for a missing document; adding it makes it READY.
3. A profile version used by a run is frozen; an update becomes v2.
4. Full sandbox lifecycle on the flagship payment: submitted ≠ credited,
   CREDIT_PENDING, then an exact credit reconciles; a fuzzy credit goes to review.
5. Evidence Pack built, verified, then tampered and re-verified.
6. Evaluation suite with release gate.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from . import country as C
from .currency import Amount
from .engine import TreasuryEngine
from .evaluation import run_suite
from .evidence import build_pack, verify_pack
from .tracking import Expected, PaymentTracker

OB = "OB-TUITION-AUT26"
USER = "student-001"


def lifecycle(e: TreasuryEngine) -> dict:
    """Plan → approval → sandbox execute → tracked lifecycle → reconciliation."""
    d = e.plan(OB)
    spec = e.intent_from_plan(OB, d)
    sel = d.selected.tranches[0]
    t = PaymentTracker(e.ledger)
    ob = e.obligations[OB]
    pay = t.create(spec.intent_id, Expected(spec.target_amount, ob.beneficiary_version_id, ob.reference,
                                            (sel.arrival_earliest, sel.arrival_latest)), e.clock.now())
    t.advance(pay.payment_id, "READY_FOR_APPROVAL", e.clock.now(), "all gates passed; above autonomous cap")
    info, token = e.request_approval(spec, USER)
    t.advance(pay.payment_id, "APPROVED", e.clock.now(), "student approved the exact fingerprint", "USER",
              {"fingerprint": spec.fingerprint()[:16]})
    res = e.execute(spec, USER, approval_token=token)
    t.advance(pay.payment_id, "SUBMITTED_SIMULATED", e.clock.now(), f"sandbox accepted {res['execution_id']}")
    e.clock.advance(hours=2)
    t.advance(pay.payment_id, "PROCESSING_SIMULATED", e.clock.now(), "simulated provider processing")
    e.clock.advance(hours=20)
    t.advance(pay.payment_id, "SENT_SIMULATED", e.clock.now(), "simulated provider sent the funds")
    pending = t.reconcile(pay.payment_id, [], e.clock.now())
    settled_before = t.obligation_settled(pay)
    e.clock.advance(days=1)
    credit = {"amount": spec.target_amount, "beneficiary_version_id": ob.beneficiary_version_id,
              "reference": ob.reference, "date": sel.arrival_earliest + timedelta(days=1), "source": "SANDBOX"}
    done = t.reconcile(pay.payment_id, [credit], e.clock.now())
    # a second, separate case: a fuzzy credit (reference typo, amount short) is never auto-closed
    pay2 = t.create("PI-FUZZY", Expected(Amount.of("45000", "CNY"), ob.beneficiary_version_id, ob.reference,
                                        (date(2026, 8, 18), date(2026, 8, 20))), e.clock.now())
    for s in ("READY_FOR_APPROVAL", "APPROVED", "SUBMITTED_SIMULATED", "PROCESSING_SIMULATED", "SENT_SIMULATED"):
        t.advance(pay2.payment_id, s, e.clock.now())
    fuzzy = t.reconcile(pay2.payment_id, [{"amount": Amount.of("44950", "CNY"), "beneficiary_version_id": ob.beneficiary_version_id,
                                           "reference": "SDU-2026-AUT-10423", "date": date(2026, 8, 19), "source": "SANDBOX"}],
                        e.clock.now())
    return {"payment": {"payment_id": pay.payment_id, "status": pay.status, "timeline": pay.timeline,
                        "settled_before_credit": settled_before, "settled": t.obligation_settled(pay),
                        "pending_check": pending, "reconciliation": done},
            "fuzzy_case": {"payment_id": pay2.payment_id, "status": pay2.status, "reconciliation": fuzzy},
            "execution": res, "approval_screen": info.get("approval_screen")}


def profile_versioning() -> dict:
    reg = C.china_pack()
    p1 = reg.latest("cn-in-demo-a")
    C.run_context("A", reg)                          # the run uses v1 → v1 is frozen
    try:
        reg.replace_in_place("cn-in-demo-a", 1, processing_buffer_bd=2)
        frozen = False
    except C.ProfileFrozen as ex:
        frozen = str(ex)
    p2 = reg.amend("cn-in-demo-a", processing_buffer_bd=4)
    return {"v1_buffer_bd": p1.processing_buffer_bd, "edit_after_use": frozen, "v2_buffer_bd": p2.processing_buffer_bd,
            "versions": [reg.get("cn-in-demo-a", v).version for v in (1, 2)],
            "old_run_keeps": "v1 (5 business-day buffer)"}


def run() -> dict:
    a = C.run_context("A")
    b = C.run_context("B")
    b_fixed = C.run_context("B", extra_documents=frozenset({"relationship_proof"}))
    unsupported = C.resolve_corridor("KZ", "DE", "STUDENT")
    ambiguous = C.resolve_corridor("", "CN", "STUDENT")
    e = TreasuryEngine()
    life = lifecycle(e)
    items = {"corridor_A": a["eligibility"], "corridor_B": b["eligibility"], "payment_timeline": life["payment"],
             "approval_screen": life["approval_screen"], "ledger_verify": e.ledger.verify()}
    pack = build_pack(items, e.ledger, e.clock.now())
    v_ok = verify_pack(pack)
    tampered = copy.deepcopy(pack)
    tampered["items"]["payment_timeline"]["status"] = "CREDITED_SIMULATED_EDITED"
    v_bad = verify_pack(tampered)
    missing = copy.deepcopy(pack)
    del missing["items"]["corridor_B"]
    v_missing = verify_pack(missing)
    suite = run_suite()
    return {"label": "SIMULATION — NO REAL MONEY MOVES", "as_of": "2026-08-17",
            "contexts": {"A": a, "B": b, "B_with_document": b_fixed},
            "resolver_examples": {"KZ→DE": unsupported, "missing sender": ambiguous},
            "research_sources": [s.to_dict() for s in C.RESEARCH_SOURCES],
            "profiles": C.china_pack().list(), "versioning": profile_versioning(),
            "pack": [C.profile_to_dict(C.inbound_profile()), C.profile_to_dict(C.outbound_profile())],
            "lifecycle": life,
            "evidence_pack": {"manifest": pack["manifest"], "verify": v_ok, "tampered_verify": v_bad,
                              "missing_item_verify": v_missing},
            "evaluation": suite}
