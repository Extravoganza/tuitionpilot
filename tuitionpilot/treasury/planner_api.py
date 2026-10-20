"""Simple-input wrapper around the 2.0 optimizer, used by the site's "Plan my payment" tool.

The site runs a JavaScript port of the same algorithm; ``tools/parity`` checks
that both give identical candidates, totals and selections on many inputs.
Everything is SIMULATED: fixture routes and quotes, fictional payee.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import fixtures as F
from .calendar import BusinessCalendar
from .currency import Amount
from .limits import Caps
from .liquidity import CashFlow, LiquidityBook
from .optimizer import Obligation, OptimizerInputs, optimize


def fixture_spec() -> dict:
    """Route parameters exported to the site (all fixtures)."""
    return {"mid": str(F.MID.value), "mid_label": F.MID.label, "ttl_min": int(F.QUOTE_TTL.total_seconds() // 60),
            "stale_routes": ["R5"], "cutoff": "15:00", "tz": F.CALENDAR.tz,
            "routes": [{"route_id": r.route_id, "name": r.name, "fixed_fee_minor": r.fixed_fee_minor,
                        "variable_fee_pct": str(r.variable_fee_pct), "intermediary_fee_minor": r.intermediary_fee_minor,
                        "fx_spread_pct": str(r.fx_spread_pct), "arrival_bd": [r.arrival_min_bd, r.arrival_max_bd]}
                       for r in F.ROUTES.values()],
            "caps": Caps().to_dict(), "caps_minor": {"autonomous": Caps().autonomous_minor, "per_payment": Caps().per_payment_minor,
                                                     "rolling_30d": Caps().rolling_30d_minor, "max_fx_share_pct": Caps().max_fx_share_pct}}


def plan_for(target_cny: str, due: str, today: str, balance_kzt: str, reserve_kzt: str,
             flows: list[tuple] = (), buffer_bd: int = 5) -> dict:
    """flows: (iso date, amount KZT string, kind) with kind confirmed_inflow | committed_outflow | forecast_inflow."""
    cal = BusinessCalendar(tz=F.CALENDAR.tz)
    d_today = date.fromisoformat(today)
    now = datetime.combine(d_today, time(10, 0), tzinfo=ZoneInfo(cal.tz))
    ob = Obligation("OB-USER", Amount.of(target_cny, "CNY"), date.fromisoformat(due), buffer_bd, "BEN", "BV-1", "REF")
    book = LiquidityBook("KZT", Amount.of(balance_kzt, "KZT"), d_today, Amount.of(reserve_kzt, "KZT"),
                         [CashFlow(date.fromisoformat(d), Amount.of(a, "KZT"), k, k) for d, a, k in flows])
    inp = OptimizerInputs(now=now, obligation=ob, quotes=F.quotes_at(now), calendar=cal, liquidity=book, caps=Caps(),
                          beneficiary_status="VERIFIED", allowed_routes=set(F.ROUTES))
    d = optimize(inp)
    return {"status": d.status, "selected": d.selected.candidate_id if d.selected else None,
            "latest_safe": d.latest_safe_arrival.isoformat(), "start": d.effective_start.isoformat(),
            "candidates": {c.candidate_id: [c.route_id, c.strategy, c.feasible, c.total.minor, c.buffer_bd,
                                            sorted(k for k, v in c.constraints.items() if not v["pass"]),
                                            [t.execution_date.isoformat() for t in c.tranches],
                                            [t.target.minor for t in c.tranches], len(c.needs_approval)]
                           for c in d.candidates},
            "savings": d.savings.get("savings") if d.savings and d.savings.get("comparable") else None}
