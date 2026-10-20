"""Deadline-safe FX conversion planner.

Goal: buy the study currency needed for an obligation before its deadline,
at a better average rate than "convert everything on the day you remember",
without ever risking the deadline.

Strategy (explainable on purpose, no black box):
  1. Split the need into N tranches scheduled evenly up to (deadline - buffer).
  2. A tranche may execute EARLY if today's rate beats the trailing 7-day
     average by at least `trigger_bp` basis points (opportunistic buy).
  3. A tranche ALWAYS executes on its scheduled date (deadline guarantee).
  4. "cautious" users convert the first tranche immediately.
This is cost averaging with an opportunistic trigger - it reduces timing
risk; it does not predict the market, and the UI says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .market import FXMarket
from .models import D, Money, q2


@dataclass
class Tranche:
    index: int
    amount_dst: Decimal
    scheduled: datetime
    done: bool = False
    executed_at: datetime | None = None
    rate: Decimal | None = None
    trigger: str | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "amount_dst": str(self.amount_dst),
                "scheduled": self.scheduled.isoformat(), "done": self.done,
                "executed_at": self.executed_at.isoformat() if self.executed_at else None,
                "rate": str(self.rate) if self.rate else None, "trigger": self.trigger}


@dataclass
class FXPlan:
    plan_id: str
    src: str
    dst: str
    need: Money
    deadline: datetime
    tranches: list = field(default_factory=list)
    trigger_bp: int = 50
    min_spacing_days: int = 4
    rationale: list = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return sum((t.amount_dst for t in self.tranches if not t.done), D(0))

    @property
    def complete(self) -> bool:
        return all(t.done for t in self.tranches)

    def to_dict(self) -> dict:
        return {"plan_id": self.plan_id, "src": self.src, "dst": self.dst,
                "need": self.need.to_dict(), "deadline": self.deadline.isoformat(),
                "trigger_bp": self.trigger_bp, "min_spacing_days": self.min_spacing_days, "tranches": [t.to_dict() for t in self.tranches],
                "remaining_dst": str(self.remaining), "rationale": self.rationale}


def make_plan(plan_id: str, src: str, need: Money, now: datetime, deadline: datetime,
              settle_buffer_days: int = 3, risk: str = "balanced", trigger_bp: int = 50) -> FXPlan:
    last = deadline - timedelta(days=settle_buffer_days)
    window_days = max(0, (last - now).days)
    n = 1 if window_days < 3 else min(4, max(2, window_days // 5))
    plan = FXPlan(plan_id, src.upper(), need.currency, need, deadline, trigger_bp=trigger_bp)
    base = q2(need.amount / n)
    amounts = [base] * (n - 1) + [need.amount - base * (n - 1)]
    for i, amt in enumerate(amounts):
        if n == 1:
            when = now
        else:
            when = now + timedelta(days=round(window_days * i / (n - 1)))
        if i == 0 and risk == "cautious":
            when = now
        plan.tranches.append(Tranche(i, q2(amt), when))
    plan.rationale = [
        f"{window_days} days available before the safety buffer ({settle_buffer_days} days before the deadline)",
        f"split into {n} tranche(s) to average out exchange-rate timing risk",
        f"a tranche executes early if the rate beats the 7-day average by >= {trigger_bp} bp",
        f"opportunistic buys are spaced at least {plan.min_spacing_days} days apart",
        "every tranche has a hard date, so the money is always in place before the deadline",
    ]
    return plan


def due_tranches(plan: FXPlan, market: FXMarket, now: datetime) -> list[tuple[Tranche, str]]:
    """Which tranches should execute now, and why."""
    out = []
    pending = [t for t in plan.tranches if not t.done]
    if not pending:
        return out
    hist = market.history(plan.src, plan.dst, now, 7)
    avg = sum(hist[:-1], D(0)) / max(1, len(hist) - 1)
    today = hist[-1]
    edge_bp = (today / avg - 1) * 10000 if avg else D(0)
    for t in pending:
        if t.scheduled <= now:
            out.append((t, "scheduled"))
    last_exec = max((t.executed_at for t in plan.tranches if t.executed_at), default=None)
    spaced = last_exec is None or (now - last_exec) >= timedelta(days=plan.min_spacing_days)
    if not out and spaced and edge_bp >= plan.trigger_bp:
        out.append((pending[0], f"opportunistic: rate {edge_bp:.0f} bp better than 7-day average"))
    return out
