"""Per-currency liquidity timeline.

liquidity(t) = opening available + confirmed inflows by t − committed outflows by t
               − planned outflows by t − minimum reserve
Forecast inflows are kept separate and never count toward feasibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .currency import Amount, CurrencyError

KINDS = {"confirmed_inflow", "forecast_inflow", "committed_outflow", "planned_outflow"}


@dataclass(frozen=True)
class CashFlow:
    on: date
    amount: Amount          # positive magnitude
    kind: str
    label: str

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown cash-flow kind {self.kind}")
        if self.amount.minor < 0:
            raise ValueError("cash-flow amounts are magnitudes; kind gives the sign")

    def signed(self) -> int:
        return self.amount.minor if self.kind.endswith("inflow") else -self.amount.minor


@dataclass
class LiquidityBook:
    ccy: str
    opening: Amount
    as_of: date
    reserve: Amount
    flows: list = field(default_factory=list)

    def __post_init__(self):
        if self.opening.ccy != self.ccy or self.reserve.ccy != self.ccy:
            raise CurrencyError("liquidity book is single-currency")

    def copy(self) -> "LiquidityBook":
        return replace(self, flows=list(self.flows))

    def projection(self, extra: list | None = None, include_forecast: bool = False) -> list[dict]:
        flows = sorted([f for f in self.flows + (extra or [])
                        if include_forecast or f.kind != "forecast_inflow"], key=lambda f: (f.on, f.signed() < 0))
        bal = self.opening.minor
        pts = [{"on": self.as_of.isoformat(), "balance_minor": bal, "event": "opening"}]
        for f in flows:
            if f.amount.ccy != self.ccy:
                raise CurrencyError(f"{f.label} is in {f.amount.ccy}, book is {self.ccy}")
            bal += f.signed()
            pts.append({"on": f.on.isoformat(), "balance_minor": bal, "event": f.label, "kind": f.kind})
        return pts

    def min_headroom(self, extra: list | None = None) -> tuple[Amount, str]:
        pts = self.projection(extra)
        low = min(pts, key=lambda p: p["balance_minor"])
        return Amount(low["balance_minor"] - self.reserve.minor, self.ccy), low["on"]

    def ok(self, extra: list | None = None) -> bool:
        return self.min_headroom(extra)[0].minor >= 0
