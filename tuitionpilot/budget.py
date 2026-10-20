"""Multi-currency budget, runway and deadline radar."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from .market import FXMarket
from .models import D, Money, Obligation, q2


def total_in(balances: dict, ccy: str, market: FXMarket, at: datetime) -> Money:
    tot = D(0)
    for c, v in balances.items():
        v = D(v)
        tot += v if c == ccy else v * market.mid(c, ccy, at)
    return Money(tot, ccy)


def budget_report(balances: dict, monthly_spend: dict, obligations: list[Obligation],
                  study_ccy: str, market: FXMarket, now: datetime) -> dict:
    """monthly_spend: {category: Money in study currency}"""
    wealth = total_in(balances, study_ccy, market, now)
    burn = sum((m.amount for m in monthly_spend.values()), D(0))
    upcoming = [o for o in obligations if not o.paid and o.due <= now + timedelta(days=60)]
    committed = D(0)
    for o in upcoming:
        a = o.amount.amount
        committed += a if o.amount.currency == study_ccy else a * market.mid(o.amount.currency, study_ccy, now)
    free = wealth.amount - committed
    runway = q2(free / burn) if burn > 0 else None
    alerts = []
    for o in sorted(upcoming, key=lambda x: x.due):
        days = (o.due - now).days
        if days <= 14:
            alerts.append(f"{o.kind.value} {o.amount} due in {days} day(s) ({o.due.date()})")
    if runway is not None and runway < 2:
        alerts.append(f"runway is {runway} months after committed payments - consider topping up or cutting spend")
    return {
        "as_of": now.isoformat(), "currency": study_ccy,
        "total_wealth": str(q2(wealth.amount)),
        "committed_next_60d": str(q2(committed)),
        "free_after_commitments": str(q2(free)),
        "monthly_burn": str(q2(burn)),
        "runway_months": str(runway) if runway is not None else None,
        "by_category": {k: str(v.amount) for k, v in monthly_spend.items()},
        "upcoming": [o.to_dict() for o in sorted(upcoming, key=lambda x: x.due)],
        "alerts": alerts,
    }
