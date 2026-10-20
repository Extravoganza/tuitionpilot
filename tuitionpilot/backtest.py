"""Monte-Carlo check of the FX planner against simple baselines.

Over many simulated rate paths, compare the cost (in home currency) of buying
the study currency with:
  A) lump sum on day 1 via bank wire (what students typically do)
  B) lump sum on day 1 via the cheapest direct route
  C) lump sum on day 1 in the wallet (same spread as the plan)
  D) TuitionPilot's deadline-safe tranche plan
Reports mean saving and the spread, so the claim is measured, not asserted.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from .bank import WALLET_FX_SPREAD
from .fxplan import due_tranches, make_plan
from .market import FXMarket
from .models import D, Money
from .rails import RAILS, quote


def run_once(seed: int, src="KZT", need=Money(45000, "CNY"), days=40):
    start = datetime(2026, 8, 1, 9)
    mkt = FXMarket(start - timedelta(days=10), seed=seed, horizon_days=days + 20)
    deadline = start + timedelta(days=days)
    wire = quote(RAILS["swift"], mkt, src, need, start).send.amount
    direct = quote(RAILS["edu"], mkt, src, need, start).send.amount
    lump = need.amount / (mkt.mid(src, need.currency, start) * (1 - WALLET_FX_SPREAD))
    late_day = deadline - timedelta(days=3)
    late_wire = quote(RAILS["swift"], mkt, src, need, late_day).send.amount
    plan = make_plan("BT", src, need, start, deadline)
    cost = D(0)
    now = start
    while not plan.complete:
        for t, why in due_tranches(plan, mkt, now):
            r = mkt.mid(src, need.currency, now) * (1 - WALLET_FX_SPREAD)
            cost += t.amount_dst / r
            t.done, t.executed_at, t.trigger = True, now, why
        now += timedelta(days=1)
        assert now <= deadline, "plan must complete before the deadline"
    return float(wire), float(direct), float(lump), float(cost), float(late_wire)


def backtest(n: int = 300) -> dict:
    rows = [run_once(s) for s in range(1, n + 1)]
    sv_wire = [(w - c) / w * 100 for w, d, l, c, lw in rows]
    sv_direct = [(d - c) / d * 100 for w, d, l, c, lw in rows]
    timing = [(l - c) / l * 100 for w, d, l, c, lw in rows]
    sv_late = [(lw - c) / lw * 100 for w, d, l, c, lw in rows]
    # timing risk: cost dispersion relative to the day-1 wallet cost
    disp_plan = statistics.pstdev([c / l * 100 for w, d, l, c, lw in rows])
    disp_late = statistics.pstdev([lw / w * 100 for w, d, l, c, lw in rows])

    def stats(xs):
        xs = sorted(xs)
        return {"mean_pct": round(statistics.mean(xs), 3), "median_pct": round(statistics.median(xs), 3),
                "p5_pct": round(xs[int(0.05 * len(xs))], 3), "p95_pct": round(xs[int(0.95 * len(xs)) - 1], 3),
                "share_worse": round(sum(1 for x in xs if x < 0) / len(xs), 3)}
    return {"paths": n, "need": "45,000 CNY from KZT, 40-day deadline",
            "saving_vs_bank_wire_day1": stats(sv_wire),
            "saving_vs_best_direct_day1": stats(sv_direct),
            "fx_timing_vs_wallet_lump_sum": stats(timing),
            "saving_vs_bank_wire_at_deadline": stats(sv_late),
            "timing_risk_stdev_pct": {"tranche_plan": round(disp_plan, 3),
                                      "single_wire_at_deadline": round(disp_late, 3),
                                      "risk_reduction_pct": round((1 - disp_plan / disp_late) * 100, 1)},
            "deadline_met": "100% of paths (asserted)",
            "note": "Simulated random-walk rates, so no strategy can predict the market: the timing effect is ~0 "
                    "on average. The measurable value is (1) lower fees from routing and (2) lower timing "
                    "risk than one conversion at the deadline. Users who want certainty can lock the full "
                    "amount on day 1 (cautious mode)."}


if __name__ == "__main__":
    import json
    print(json.dumps(backtest(), indent=2))
