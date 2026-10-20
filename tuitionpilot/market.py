"""Sandbox FX market.

Rates are SIMULATED (seeded geometric random walk around reference mid-rates).
They are not live prices and must never be presented as such.
"""
from __future__ import annotations

import math
import random
from datetime import datetime
from decimal import Decimal

from .models import D

# Reference mid-rates: units of currency per 1 USD (approximate, for simulation only).
REFERENCE_PER_USD = {
    "USD": 1.0, "CNY": 7.10, "EUR": 0.92, "GBP": 0.78, "KZT": 480.0,
    "IDR": 16000.0, "INR": 84.0, "VND": 25000.0, "THB": 35.0, "MYR": 4.6,
    "UZS": 12700.0, "RUB": 82.0, "BRL": 5.5, "NGN": 1550.0, "HKD": 7.8,
}
# Daily volatility per currency vs USD (simulation parameter).
DAILY_VOL = {"USD": 0.0, "CNY": 0.002, "HKD": 0.0005, "EUR": 0.004, "GBP": 0.005}
DEFAULT_VOL = 0.007


class FXMarket:
    def __init__(self, start: datetime, seed: int = 42, horizon_days: int = 120):
        self.start = start
        self.seed = seed
        self.horizon = horizon_days
        self._paths: dict[str, list[float]] = {}
        rng = random.Random(seed)
        for ccy, ref in REFERENCE_PER_USD.items():
            vol = DAILY_VOL.get(ccy, DEFAULT_VOL)
            path = [ref]
            for _ in range(horizon_days * 24):           # hourly steps
                shock = rng.gauss(0, vol / math.sqrt(24))
                path.append(path[-1] * math.exp(shock))
            self._paths[ccy] = path

    def supported(self) -> list[str]:
        return sorted(self._paths)

    def _idx(self, at: datetime) -> int:
        h = int((at - self.start).total_seconds() // 3600)
        return max(0, min(h, self.horizon * 24))

    def per_usd(self, ccy: str, at: datetime) -> float:
        ccy = ccy.upper()
        if ccy not in self._paths:
            raise KeyError(f"unsupported currency {ccy}")
        return self._paths[ccy][self._idx(at)]

    def mid(self, base: str, quote: str, at: datetime) -> Decimal:
        """How many units of `quote` you get for 1 unit of `base` (mid-market)."""
        r = self.per_usd(quote, at) / self.per_usd(base, at)
        return D(f"{r:.8f}")

    def history(self, base: str, quote: str, at: datetime, days: int) -> list[Decimal]:
        """Daily mid-rate history ending at `at` (inclusive), oldest first."""
        out = []
        end = self._idx(at)
        for d in range(days, -1, -1):
            i = max(0, end - d * 24)
            r = self._paths[quote.upper()][i] / self._paths[base.upper()][i]
            out.append(D(f"{r:.8f}"))
        return out
