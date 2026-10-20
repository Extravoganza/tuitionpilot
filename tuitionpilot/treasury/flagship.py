"""Flagship demo (spec §17, brief "Flagship demo"), fully deterministic for a seed.

 1. Load the fictional 45,000 CNY invoice.
 2. Parse it as untrusted data; compare with the verified beneficiary.
 3. Compare five fictional routes with fee breakdowns.
 4. Generate all-at-once and tranche plans; pick the cheapest feasible plan.
 5. Stress FX ±5% and a 3-business-day delay.
 6. Inject changed beneficiary details; the payment is blocked.
 7. The payment above the autonomous cap requires an exact approval.
 8. Change the amount after approval; the approval is invalidated.
 9. Execute the simulated payment once; time out, retry, prove no duplicate.
10. Verify the hash chain, checkpoint it, and replay the run.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

from .currency import Amount
from .engine import SimulatedTimeout, TreasuryEngine
from .ledger import canonical_json
from .stress import run_stress
from . import fixtures as F

OB = "OB-TUITION-AUT26"


def run(seed: int = 7) -> dict:
    e = TreasuryEngine(seed=seed)
    user = e.user_id
    steps = []

    def step(n, title, result, claim):
        steps.append({"step": n, "title": title, "claim": claim, "result": result})

    inv = e.parse_invoice(F.TUITION_INVOICE)
    step(1, "Load invoice", {"invoice_id": inv["invoice_id"], "extracted": inv["extracted"]},
         "A fictional invoice for 45,000.00 CNY due 2026-09-03 is loaded.")
    step(2, "Parse as untrusted data and compare with the verified payee", inv["shield"],
         f"Invoice fields match the independently verified beneficiary version ({inv['shield']['status']}).")

    d = e.plan(OB)
    quotes = []
    for c in d.candidates:
        if c.strategy == "all_at_once":
            t = c.tranches[0]
            quotes.append({"route_id": c.route_id, "route": c.route_name, "cost": t.cost.to_dict(),
                           "arrival_range": [t.arrival_earliest.isoformat(), t.arrival_latest.isoformat()],
                           "feasible": c.feasible, "failed": c.failed()})
    step(3, "Compare five routes", {"routes": quotes, "quotes": d.quotes},
         "Five routes are itemised. R4 is cheapest but late; R5's quote is stale; both are rejected.")
    step(4, "Generate plans and select", {"status": d.status, "selected": d.selected.to_dict(),
                                         "rejected": d.rejected(), "savings": d.savings, "assumptions": d.assumptions},
         f"Selected {d.selected.route_id} {d.selected.strategy} at {d.selected.total}; "
         f"{sum(1 for c in d.candidates if not c.feasible)} of {len(d.candidates)} candidates are infeasible.")

    stress = run_stress(e, OB)
    picked = [r for r in stress["scenarios"] if r["scenario"] in ("FX +5%", "FX -5%", "Delay 3 business days")]
    step(5, "Stress FX ±5% and 3-day delay", {"scenarios": picked}, "Scenario outputs, not forecasts.")

    bad = e.parse_invoice(F.CHANGED_INVOICE)
    bad_spec = e.intent_from_invoice(bad["invoice_id"], OB, d)
    blocked = e.execute(bad_spec, user)
    quiet = e.parse_invoice(F.CHANGED_INVOICE_CLEAN)
    quiet_spec = e.intent_from_invoice(quiet["invoice_id"], OB, d)
    blocked_quiet = e.execute(quiet_spec, user)
    step(6, "Changed beneficiary details are blocked",
         {"phishing": {"shield": bad["shield"], "flags": bad["flags"], "execution": blocked},
          "polite_change": {"shield": quiet["shield"], "flags": quiet["flags"], "execution": blocked_quiet}},
         "A phishing invoice is denied for prompt injection (POL-018). A polite invoice with a new account "
         "creates a new, unverified beneficiary version and is denied too (POL-010); even if the student "
         "switched to it, a 48-hour hold applies until an independent callback (POL-011).")

    spec = e.intent_from_plan(OB, d)
    direct = e.execute(spec, user)
    info, token = e.request_approval(spec, user)
    step(7, "Above the autonomous cap: exact approval required",
         {"attempt_without_approval": direct, "approval": info},
         "Without approval the payment does not execute; an exact-action challenge is issued.")

    mutated = replace(spec, source_amount=Amount(spec.source_amount.minor + 100_000, spec.source_amount.ccy))
    mut = e.execute(mutated, user, approval_token=token)
    info2, token2 = e.request_approval(spec, user)
    step(8, "Amount changed after approval: approval invalidated",
         {"mutated_amount": str(mutated.source_amount), "result": mut,
          "first_challenge_status": e.approvals.challenges[info["challenge"]["approval_id"]].status,
          "new_challenge": info2["challenge"]},
         "The original approval no longer redeems; a new one is required for the exact action.")

    try:
        e.execute(spec, user, approval_token=token2, simulate_timeout=True)
        timeout = False
    except SimulatedTimeout:
        timeout = True
    retry1 = e.execute(spec, user, approval_token=token2)
    retry2 = e.execute(spec, user)
    execs = e.executions()
    debits = [f for f in e.liquidity.flows if f.kind == "committed_outflow" and "(simulated)" in f.label]
    step(9, "Execute once, time out, retry: no duplicate",
         {"timeout_raised": timeout, "retry_1": retry1, "retry_2": retry2,
          "executions": [x.to_dict() for x in execs], "simulated_debits": len(debits)},
         "One execution record, one simulated debit, after a timeout and two retries.")

    cp = e.ledger.checkpoint()
    v = e.ledger.verify()
    step(10, "Verify the hash chain", {"verify": v, "checkpoint": cp.__dict__},
         "Every event links to the previous one; a checkpoint is exported for tail-deletion checks.")

    evidence = {"seed": seed, "label": "SIMULATION — NO REAL MONEY MOVES", "steps": steps,
                "stress": stress["scenarios"], "ledger_events": len(e.ledger.events),
                "policy_version": e.ledger.events[0]["policy_version"], "app_version": e.ledger.events[0]["app_version"]}
    return {"evidence": evidence, "engine": e}


def output_hash(evidence: dict) -> str:
    return hashlib.sha256(canonical_json(evidence).encode()).hexdigest()


def replay(seed: int = 7) -> dict:
    a, b = run(seed), run(seed)
    ha, hb = output_hash(a["evidence"]), output_hash(b["evidence"])
    la, lb = a["engine"].ledger.verify()["head_hash"], b["engine"].ledger.verify()["head_hash"]
    return {"seed": seed, "output_hash_run1": ha, "output_hash_run2": hb, "outputs_identical": ha == hb,
            "ledger_head_run1": la, "ledger_head_run2": lb, "ledgers_identical": la == lb}
