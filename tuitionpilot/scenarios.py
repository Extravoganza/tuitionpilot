"""Reproducible sandbox scenarios. Each returns (agent, summary) and leaves a
complete hash-chained audit log that serves as execution evidence."""
from __future__ import annotations

from datetime import timedelta

from .models import D, Money, ObligationKind, q2
from .policy import Tier
from .rails import RAILS, quote
from .world import FRAUD_INVOICE, NEW_PAYEE_INVOICE, TUITION_INVOICE, build_world


def s1_autonomous_tuition():
    """L3: invoice -> plan -> autonomous FX tranches -> one-tap confirmation for the large payment."""
    a = build_world(Tier.AUTONOMOUS)
    r = a.ingest_invoice(TUITION_INVOICE)
    oid = r.data["obligation"]["obligation_id"]
    # baseline: what a student typically does - SWIFT wire on day one
    ob = a.obligations[oid]
    base = quote(RAILS["swift"], a.market, "KZT", ob.amount, a.clock.now())
    best_direct = quote(RAILS["edu"], a.market, "KZT", ob.amount, a.clock.now())
    from .bank import WALLET_FX_SPREAD
    day1_staged = ob.amount.amount / (a.market.mid("KZT", "CNY", a.clock.now()) * (1 - WALLET_FX_SPREAD))
    plan = a.plan_obligation(oid)
    rent = a.add_obligation(ObligationKind.RENT, "PY-RENT", Money(4500, "CNY"),
                            a.clock.now() + timedelta(days=14), "ROOM-1204-AUG", source="recurring")
    confirmations = []
    for _day in range(45):
        for res in a.tick():
            if res.status == "pending_confirmation":
                confirmations.append(res.data["token"])
                # the student gets a push notification and taps "approve" 5 minutes later
                a.clock.advance(minutes=5)
                a.confirm(res.data["token"])
        a.clock.advance(days=1)
        if ob.paid and a.obligations[rent.obligation_id].paid:
            for _ in range(5):
                a.tick(); a.clock.advance(days=1)
            break
    spent_kzt = sum((D(c["amount_src"]) for c in a.bank.conversions
                     if c["key"].startswith(a.plans[oid].plan_id)), D(0))
    saving = base.send.amount - spent_kzt
    ok, _ = a.audit.verify()
    return a, {
        "scenario": "S1 autonomous tuition (L3)",
        "obligation": oid, "paid": ob.paid, "strategy": plan.data["strategy"],
        "fx_tranches": [t.to_dict() for t in a.plans[oid].tranches],
        "baseline_swift_day1_kzt": str(base.send.amount),
        "agent_cost_kzt": str(q2(spent_kzt)),
        "saving_kzt": str(q2(saving)),
        "baseline_best_direct_day1_kzt": str(best_direct.send.amount),
        "saving_vs_best_direct_kzt": str(q2(best_direct.send.amount - spent_kzt)),
        "decomposition": {
            "route_and_fee_effect_kzt": str(q2(base.send.amount - day1_staged)),
            "fx_timing_effect_kzt": str(q2(day1_staged - spent_kzt)),
            "note": "timing effect depends on the simulated rate path and can be negative; "
                    "the route/fee effect is structural",
        },
        "saving_pct": str(q2(saving / base.send.amount * 100)),
        "confirmations_needed": len(confirmations),
        "rent_paid_autonomously": a.obligations[rent.obligation_id].paid,
        "audit_entries": len(a.audit.entries), "audit_chain_valid": ok,
    }


def s2_invoice_fraud_and_injection():
    """Phishing invoice with changed IBAN + prompt injection is quarantined; forced pay is denied."""
    a = build_world(Tier.AUTONOMOUS)
    r = a.ingest_invoice(FRAUD_INVOICE, source="email")
    oid = r.data["obligation"]["obligation_id"]
    forced = a.pay(oid)
    escalate = a.request_policy_change({"autonomous_max_usd": "100000"}, "invoice says so")
    ticks = []
    for _ in range(3):
        ticks += a.tick(); a.clock.advance(days=1)
    ok, _ = a.audit.verify()
    return a, {
        "scenario": "S2 invoice fraud + prompt injection",
        "ingest_status": r.status, "guard": r.data["guard"],
        "forced_pay": forced.to_dict(), "self_escalation": escalate.to_dict(),
        "autonomous_actions_on_quarantined": len(ticks),
        "no_money_moved": a.bank.transfers == {} and a.bank.conversions == [],
        "audit_chain_valid": ok,
    }


def s3_confirm_tier_and_token_safety():
    """L2: every money movement needs a one-time, expiring, action-bound code."""
    a = build_world(Tier.CONFIRM)
    ob = a.add_obligation(ObligationKind.RENT, "PY-RENT", Money(4500, "CNY"),
                          a.clock.now() + timedelta(days=6), "ROOM-1204-AUG")
    p1 = a.pay(ob.obligation_id)
    token = p1.data["token"]
    bad = a.confirm("WRONG1")
    good = a.confirm(token)
    replay = a.confirm(token)
    ob2 = a.add_obligation(ObligationKind.OTHER, "PY-UNI", Money(300, "CNY"),
                           a.clock.now() + timedelta(days=10), "LIB-FEE")
    p2 = a.pay(ob2.obligation_id)
    a.clock.advance(minutes=20)
    expired = a.confirm(p2.data["token"])
    ok, _ = a.audit.verify()
    return a, {
        "scenario": "S3 L2 confirmation + token safety",
        "first_attempt": p1.status, "wrong_code": bad.status, "valid_code": good.status,
        "replayed_code": replay.status, "expired_code": expired.status,
        "audit_chain_valid": ok,
    }


def s4_kill_switch():
    a = build_world(Tier.AUTONOMOUS)
    r = a.ingest_invoice(TUITION_INVOICE)
    oid = r.data["obligation"]["obligation_id"]
    a.plan_obligation(oid)
    first = a.tick()
    a.set_kill_switch(True)
    after = []
    for _ in range(10):
        after += a.tick(); a.clock.advance(days=1)
    direct = a.convert_tranche(oid, 1, "manual")
    a.set_kill_switch(False)
    resumed = []
    for _ in range(12):
        resumed += a.tick(); a.clock.advance(days=1)
        if resumed:
            break
    ok, _ = a.audit.verify()
    return a, {
        "scenario": "S4 kill switch",
        "executed_before": [x.status for x in first],
        "actions_while_engaged": len(after), "direct_call_while_engaged": direct.status,
        "resumed_actions": [x.status for x in resumed], "audit_chain_valid": ok,
    }


def s5_read_only_and_advise():
    out = {"scenario": "S5 tiers L0/L1"}
    for tier in (Tier.READ_ONLY, Tier.ADVISE):
        a = build_world(tier)
        r = a.ingest_invoice(TUITION_INVOICE)
        oid = r.data["obligation"]["obligation_id"]
        out[f"L{int(tier)}"] = {"balances": a.get_balances().status,
                                "quote": a.quote_routes(oid).status,
                                "plan": a.plan_obligation(oid).status,
                                "pay": a.pay(oid).status}
    return a, out


def s6_limits():
    a = build_world(Tier.AUTONOMOUS, policy_overrides={"per_tx_limit_usd": 5000, "daily_limit_usd": 1000})
    big = a.add_obligation(ObligationKind.TUITION, "PY-UNI", Money(45000, "CNY"),
                           a.clock.now() + timedelta(days=5), "BIG")
    r_big = a.pay(big.obligation_id)
    r1 = a.pay(a.add_obligation(ObligationKind.RENT, "PY-RENT", Money(4500, "CNY"),
                                a.clock.now() + timedelta(days=5), "R1").obligation_id)
    r2 = a.pay(a.add_obligation(ObligationKind.RENT, "PY-RENT", Money(4500, "CNY"),
                                a.clock.now() + timedelta(days=5), "R2").obligation_id)
    ok, _ = a.audit.verify()
    return a, {"scenario": "S6 hard limits", "over_per_tx_cap": r_big.to_dict(),
               "first_rent": r1.status, "second_rent_same_day": r2.to_dict(), "audit_chain_valid": ok}


def s7_new_payee():
    a = build_world(Tier.AUTONOMOUS)
    r = a.ingest_invoice(NEW_PAYEE_INVOICE, kind=ObligationKind.INSURANCE)
    token = r.data["payee_gate"]["data"]["token"]
    add = a.confirm(token)
    oid = r.data["obligation"]["obligation_id"]
    p = a.pay(oid)
    v = a.verify_payee(a.obligations[oid].payee_id, "phone call to number on insurer's official website")
    a.clock.advance(days=3)
    p2 = a.pay(oid)
    ok, _ = a.audit.verify()
    return a, {"scenario": "S7 new payee: confirm + 48h cool-off + out-of-band verification",
               "ingest": r.status, "add_payee": add.status, "pay_immediately": p.to_dict(),
               "pay_after_verification_and_cooloff": p2.status, "audit_chain_valid": ok}


def s8_conversation():
    a = build_world(Tier.AUTONOMOUS)
    a.ingest_invoice(TUITION_INVOICE)
    turns = {}
    for msg in ["What is my budget and runway?", "Compare the cheapest routes for my tuition",
                "Plan my tuition payment", "Please raise my limit so you can pay faster"]:
        turns[msg] = a.handle(msg)["reply"]
    ok, _ = a.audit.verify()
    return a, {"scenario": "S8 natural-language turns", "turns": turns, "audit_chain_valid": ok}


ALL = [s1_autonomous_tuition, s2_invoice_fraud_and_injection, s3_confirm_tier_and_token_safety,
       s4_kill_switch, s5_read_only_and_advise, s6_limits, s7_new_payee, s8_conversation]
