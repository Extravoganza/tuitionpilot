"""Route quotes with explicit direction, timestamp, TTL, source and fee itemisation.

All-in source cost (spec §5):
    principal_source  = target / (mid × (1 − fx_spread))          rounded up
    principal_at_mid  = target / mid                                rounded up
    fx_spread_cost    = principal_source − principal_at_mid
    variable_fee      = principal_source × variable_fee_pct         rounded up
    all_in_known      = principal_source + variable_fee + fixed_fee + intermediary_fee
    effective_rate    = all_in_known / target   labelled "<source> per 1 <target>, all-in"
Estimated or unknown costs (e.g. receiving-bank charges) are listed separately
and never folded into the known total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

from .currency import Amount, CurrencyError, Rate, to_decimal

DATA_CLASSES = {"SIMULATED", "ESTIMATE", "CONFIRMED_QUOTE"}


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    name: str
    fixed_fee_minor: int
    variable_fee_pct: Decimal
    intermediary_fee_minor: int
    fx_spread_pct: Decimal
    arrival_min_bd: int
    arrival_max_bd: int
    supports_reference: bool = True
    estimated_extra_note: str = "receiving-bank charges, if any, are not known (TBD)"

    def to_dict(self) -> dict:
        return {"route_id": self.route_id, "name": self.name, "fixed_fee_minor": self.fixed_fee_minor,
                "variable_fee_pct": str(self.variable_fee_pct), "intermediary_fee_minor": self.intermediary_fee_minor,
                "fx_spread_pct": str(self.fx_spread_pct), "arrival_bd": [self.arrival_min_bd, self.arrival_max_bd],
                "supports_reference": self.supports_reference}


@dataclass(frozen=True)
class RouteQuote:
    quote_id: str
    route: RouteSpec
    mid: Rate                    # target per 1 source, e.g. CNY per 1 KZT
    quoted_at: datetime          # timezone-aware
    ttl: timedelta
    source: str = "FIXTURE"
    data_class: str = "SIMULATED"

    def __post_init__(self):
        if self.quoted_at is None or self.quoted_at.tzinfo is None:
            raise ValueError("quote timestamp missing or not timezone-aware")
        if self.data_class not in DATA_CLASSES:
            raise ValueError("unknown data class")

    @property
    def source_ccy(self) -> str:
        return self.mid.base

    @property
    def target_ccy(self) -> str:
        return self.mid.quote

    def age(self, now: datetime) -> timedelta:
        return now - self.quoted_at

    def expires_at(self) -> datetime:
        return self.quoted_at + self.ttl

    def is_fresh(self, now: datetime) -> bool:
        return timedelta(0) <= self.age(now) <= self.ttl

    def freshness(self, now: datetime) -> str:
        return "FRESH" if self.is_fresh(now) else "STALE"

    def to_dict(self, now: datetime | None = None) -> dict:
        d = {"quote_id": self.quote_id, "route": self.route.to_dict(), "mid": self.mid.to_dict(),
             "quoted_at": self.quoted_at.astimezone(timezone.utc).isoformat(),
             "expires_at": self.expires_at().astimezone(timezone.utc).isoformat(),
             "source": self.source, "data_class": self.data_class}
        if now is not None:
            d["age_minutes"] = int(self.age(now).total_seconds() // 60)
            d["freshness"] = self.freshness(now)
        return d


@dataclass(frozen=True)
class CostBreakdown:
    target: Amount
    principal_at_mid: Amount
    fx_spread_cost: Amount
    variable_fee: Amount
    fixed_fee: Amount
    intermediary_fee: Amount
    all_in_known: Amount
    effective_rate: Decimal          # source per 1 target, all-in
    effective_rate_label: str
    rate_used: Rate                  # rate after spread
    estimated_extras: tuple = field(default_factory=tuple)

    @property
    def principal_source(self) -> Amount:
        return self.principal_at_mid + self.fx_spread_cost

    def to_dict(self) -> dict:
        return {"target": self.target.to_dict(), "principal_at_mid": self.principal_at_mid.to_dict(),
                "fx_spread_cost": self.fx_spread_cost.to_dict(), "variable_fee": self.variable_fee.to_dict(),
                "fixed_fee": self.fixed_fee.to_dict(), "intermediary_fee": self.intermediary_fee.to_dict(),
                "all_in_known": self.all_in_known.to_dict(),
                "effective_rate": f"{self.effective_rate:.6f}", "effective_rate_label": self.effective_rate_label,
                "rate_used": self.rate_used.to_dict(), "estimated_extras": list(self.estimated_extras)}


def cost_for_target(q: RouteQuote, target: Amount, rate_shock: Decimal = Decimal(0)) -> CostBreakdown:
    """Known all-in source cost for the payee to receive ``target``.

    ``rate_shock`` is a scenario input for stress tests: +0.05 means the source
    currency is 5% weaker at execution (more source needed). Base cost uses 0.
    """
    if target.ccy != q.target_ccy:
        raise CurrencyError(f"quote delivers {q.target_ccy}, target is {target.ccy}")
    shock = to_decimal(rate_shock)
    mid_value = q.mid.value / (Decimal(1) + shock)
    mid = Rate(q.mid.base, q.mid.quote, mid_value)
    after_spread = Rate(q.mid.base, q.mid.quote, mid_value * (Decimal(1) - q.route.fx_spread_pct))
    principal_source = after_spread.source_needed(target)
    principal_at_mid = mid.source_needed(target)
    spread = principal_source - principal_at_mid
    variable = principal_source.scale(q.route.variable_fee_pct, rounding=ROUND_CEILING)
    fixed = Amount(q.route.fixed_fee_minor, q.source_ccy)
    inter = Amount(q.route.intermediary_fee_minor, q.source_ccy)
    total = principal_source + variable + fixed + inter
    eff = (total.major / target.major) if target.minor else Decimal(0)
    return CostBreakdown(target, principal_at_mid, spread, variable, fixed, inter, total, eff,
                         f"{q.source_ccy} per 1 {q.target_ccy}, all-in", after_spread,
                         (q.route.estimated_extra_note,))
