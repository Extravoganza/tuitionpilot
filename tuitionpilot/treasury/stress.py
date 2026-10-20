"""Stress and counterfactual lab.

Each scenario changes inputs explicitly and re-runs the optimizer. Results show
the effect on plan, cost, liquidity headroom and deadline buffer. No
probabilities are attached: the model is not calibrated for that.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .liquidity import CashFlow
from .optimizer import optimize


def _summary(name: str, group: str, change: str, d, base_total=None) -> dict:
    sel = d.selected
    row = {"scenario": name, "group": group, "change": change, "status": d.status,
           "selected": f"{sel.route_id} {sel.strategy}" if sel else None,
           "total": str(sel.total) if sel else None, "total_minor": sel.total.minor if sel else None,
           "deadline_buffer_bd": sel.buffer_bd if sel else None,
           "lowest_headroom": sel.constraints["liquidity_reserve"]["detail"] if sel else None,
           "feasible_candidates": sum(1 for c in d.candidates if c.feasible),
           "reasons_if_infeasible": d.reasons_if_infeasible}
    if sel and base_total is not None:
        row["cost_delta_vs_base"] = sel.total.minor - base_total
    return row


def run_stress(engine, obligation_id: str) -> dict:
    base_inp = engine.optimizer_inputs(obligation_id)
    base = optimize(base_inp)
    bt = base.selected.total.minor if base.selected else None
    rows = [_summary("Base case", "base", "fixtures as quoted", base, bt)]

    for s in ("0.01", "0.03", "0.05", "0.10"):
        for sign, word in ((1, "weaker"), (-1, "stronger")):
            shock = Decimal(s) * sign
            d = optimize(replace(base_inp, rate_shock=shock))
            rows.append(_summary(f"FX {'+' if sign > 0 else '-'}{int(Decimal(s) * 100)}%", "fx",
                                 f"KZT {word} by {int(Decimal(s) * 100)}% for tranches after the first execution date", d, bt))
    for n in (1, 3, 5):
        d = optimize(replace(base_inp, extra_delay_bd=n))
        rows.append(_summary(f"Delay {n} business day{'s' if n > 1 else ''}", "delay",
                             f"every route arrives {n} business day(s) later", d, bt))

    liq = base_inp.liquidity
    early = liq.copy()
    early.flows = [replace(f, on=base.effective_start) if f.kind == "committed_outflow" and "Rent" in f.label else f
                   for f in early.flows]
    rows.append(_summary("Early rent", "liquidity", "rent and living costs due on the first day", optimize(replace(base_inp, liquidity=early)), bt))
    late = liq.copy()
    late.flows = [replace(f, on=f.on + timedelta(days=10)) if f.kind == "confirmed_inflow" else f for f in late.flows]
    rows.append(_summary("Sponsor funds 10 days late", "liquidity", "confirmed sponsor transfer arrives 10 days later",
                         optimize(replace(base_inp, liquidity=late)), bt))
    sel_route = base.selected.route_id if base.selected else "R3"
    rows.append(_summary(f"Route outage {sel_route}", "operations", f"route {sel_route} unavailable",
                         optimize(replace(base_inp, outage={sel_route})), bt))
    rows.append(_summary("Quotes go stale", "data", "60 minutes pass without a quote refresh",
                         optimize(replace(base_inp, now=base_inp.now + timedelta(minutes=60))), bt))
    rows.append(_summary("Beneficiary details changed", "fraud", "latest payee version is unverified",
                         optimize(replace(base_inp, beneficiary_status="HOLD")), bt))
    early_start = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)
    from . import fixtures as F
    cf = optimize(replace(base_inp, now=early_start, quotes=F.quotes_at(early_start),
                          liquidity=replace(liq, as_of=early_start.date())))
    rows.append(_summary("Counterfactual: start on 1 Aug", "counterfactual",
                         "planning 16 days earlier (workbook dates)", cf, bt))
    return {"base": base.to_dict(), "scenarios": rows,
            "note": "Scenario outputs, not forecasts. No probabilities are implied.",
            "label": "SIMULATION — NO REAL MONEY MOVES"}
