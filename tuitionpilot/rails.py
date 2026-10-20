"""Payment rails catalogue and quoting.

Fee schedules are illustrative sandbox parameters modelled on publicly typical
pricing structures (fixed fee + FX markup + settlement time). They do not
represent any specific provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .market import FXMarket
from .models import D, Money, q2


@dataclass(frozen=True)
class Rail:
    rail_id: str
    name: str
    fixed_fee_usd: Decimal
    pct_fee: Decimal          # provider fee on send amount
    fx_markup: Decimal        # spread over mid-market
    settle_days: int
    max_send_usd: Decimal
    reliability: Decimal      # historical success rate (sandbox parameter)
    supports_reference: bool  # can carry a payment reference (needed for tuition)


RAILS: dict[str, Rail] = {
    "swift": Rail("swift", "Bank wire (SWIFT)", D(25), D("0"), D("0.015"), 4, D(1_000_000), D("0.97"), True),
    "remit": Rail("remit", "Licensed remittance provider", D(3), D("0.006"), D("0.004"), 1, D(10_000), D("0.99"), True),
    "edu":   Rail("edu", "Education payment platform", D(0), D("0.009"), D("0.003"), 2, D(100_000), D("0.995"), True),
    "card":  Rail("card", "Debit/credit card", D(0), D("0"), D("0.03"), 0, D(3_000), D("0.98"), False),
    "local": Rail("local", "Local in-currency transfer", D(0), D("0"), D("0"), 0, D(50_000), D("0.999"), True),
}


@dataclass
class Quote:
    rail_id: str
    rail_name: str
    send: Money               # debited in source currency
    receive: Money            # credited to payee
    fees_in_source: Money
    effective_rate: Decimal
    mid_rate: Decimal
    hidden_cost_pct: Decimal  # total cost vs mid-market, %
    arrives_by: datetime
    reliability: Decimal
    eligible: bool
    reasons: list

    def to_dict(self) -> dict:
        return {
            "rail_id": self.rail_id, "rail_name": self.rail_name,
            "send": self.send.to_dict(), "receive": self.receive.to_dict(),
            "fees_in_source": self.fees_in_source.to_dict(),
            "effective_rate": str(self.effective_rate), "mid_rate": str(self.mid_rate),
            "total_cost_vs_mid_pct": str(self.hidden_cost_pct),
            "arrives_by": self.arrives_by.isoformat(),
            "reliability": str(self.reliability), "eligible": self.eligible,
            "reasons": self.reasons,
        }


def quote(rail: Rail, market: FXMarket, src_ccy: str, target: Money, at: datetime,
          needs_reference: bool = True, deadline: datetime | None = None) -> Quote:
    """Quote how much `src_ccy` must be sent over `rail` so the payee receives `target`."""
    src_ccy = src_ccy.upper()
    reasons: list[str] = []
    eligible = True
    if rail.rail_id == "local" and src_ccy != target.currency:
        eligible = False
        reasons.append("local rail requires same currency")
    mid = market.mid(src_ccy, target.currency, at) if src_ccy != target.currency else D(1)
    eff = mid * (1 - rail.fx_markup) if src_ccy != target.currency else D(1)
    usd_per_src = market.mid(src_ccy, "USD", at)
    fixed_src = rail.fixed_fee_usd / usd_per_src if usd_per_src else D(0)
    principal_src = target.amount / eff
    send_src = (principal_src + fixed_src) / (1 - rail.pct_fee)
    fees_src = send_src - target.amount / mid
    send_usd = send_src * usd_per_src
    if send_usd > rail.max_send_usd:
        eligible = False
        reasons.append(f"exceeds rail limit ({rail.max_send_usd} USD)")
    if needs_reference and not rail.supports_reference:
        eligible = False
        reasons.append("cannot carry payment reference; university may not match payment")
    arrives = at + timedelta(days=rail.settle_days)
    if deadline is not None and arrives > deadline:
        eligible = False
        reasons.append("would arrive after the deadline")
    cost_pct = (fees_src / (target.amount / mid) * 100) if mid else D(0)
    return Quote(rail.rail_id, rail.name, Money(send_src, src_ccy), target,
                 Money(fees_src, src_ccy), D(f"{eff:.8f}"), mid, q2(cost_pct),
                 arrives, rail.reliability, eligible, reasons)


def rank_routes(market: FXMarket, src_ccy: str, target: Money, at: datetime,
                deadline: datetime | None = None, needs_reference: bool = True,
                allowed: list[str] | None = None) -> list[Quote]:
    """Return all quotes, eligible first, cheapest (risk-adjusted) first."""
    quotes = []
    for rid, rail in RAILS.items():
        if allowed is not None and rid not in allowed:
            continue
        quotes.append(quote(rail, market, src_ccy, target, at, needs_reference, deadline))

    def score(qt: Quote):
        # expected cost = send amount, penalised by failure probability (a failed
        # tuition payment can cost a late fee or enrolment hold)
        risk_penalty = (1 - qt.reliability) * qt.send.amount * D("0.5")
        return (not qt.eligible, qt.send.amount + risk_penalty)

    return sorted(quotes, key=score)
