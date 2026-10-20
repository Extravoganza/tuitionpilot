"""Concurrency-safe limit reservations (POL-006/007/008).

``try_reserve`` checks and reserves under one lock, so two parallel requests
can never both pass against the same remaining cap. Reservations become
spend on ``commit`` and are freed on ``release``. In-process only: a database
with transactions is required before multi-process deployment (ADR-001).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .currency import Amount


@dataclass
class Caps:
    ccy: str = "KZT"
    autonomous_minor: int = 100_000_000          # 1,000,000.00 KZT per action without approval (L3)
    per_payment_minor: int = 750_000_000         # 7,500,000.00 KZT hard cap
    daily_minor: int = 1_000_000_000             # 10,000,000.00 KZT
    rolling_30d_minor: int = 1_500_000_000       # 15,000,000.00 KZT
    max_fx_share_pct: int = 60                   # one FX buy ≤ 60% of the obligation

    def to_dict(self) -> dict:
        f = lambda m: str(Amount(m, self.ccy))
        return {"currency": self.ccy, "autonomous": f(self.autonomous_minor), "per_payment": f(self.per_payment_minor),
                "daily": f(self.daily_minor), "rolling_30d": f(self.rolling_30d_minor),
                "max_fx_share_pct": self.max_fx_share_pct}


@dataclass
class Reservation:
    reservation_id: str
    intent_id: str
    amount: Amount
    at: datetime
    state: str = "RESERVED"     # RESERVED | COMMITTED | RELEASED


class LimitBook:
    def __init__(self, caps: Caps):
        self.caps = caps
        self._lock = threading.Lock()
        self._items: list[Reservation] = []
        self._n = 0

    def _window_total(self, at: datetime, span: timedelta) -> int:
        return sum(r.amount.minor for r in self._items
                   if r.state in ("RESERVED", "COMMITTED") and at - span < r.at <= at + timedelta(seconds=1))

    def headroom(self, at: datetime) -> dict:
        with self._lock:
            return {"daily_used": self._window_total(at, timedelta(days=1)),
                    "rolling_30d_used": self._window_total(at, timedelta(days=30))}

    def try_reserve(self, intent_id: str, amount: Amount, at: datetime) -> tuple[Reservation | None, list[str]]:
        if amount.ccy != self.caps.ccy:
            return None, ["CAP_CURRENCY_MISMATCH"]
        with self._lock:
            for r in self._items:
                if r.intent_id == intent_id and r.state in ("RESERVED", "COMMITTED"):
                    return r, []                          # idempotent
            reasons = []
            if amount.minor > self.caps.per_payment_minor:
                reasons.append("PER_PAYMENT_CAP_EXCEEDED")
            if self._window_total(at, timedelta(days=1)) + amount.minor > self.caps.daily_minor:
                reasons.append("DAILY_CAP_EXCEEDED")
            if self._window_total(at, timedelta(days=30)) + amount.minor > self.caps.rolling_30d_minor:
                reasons.append("ROLLING_30D_CAP_EXCEEDED")
            if reasons:
                return None, reasons
            self._n += 1
            r = Reservation(f"RSV-{self._n:04d}", intent_id, amount, at)
            self._items.append(r)
            return r, []

    def commit(self, reservation_id: str) -> None:
        with self._lock:
            for r in self._items:
                if r.reservation_id == reservation_id:
                    r.state = "COMMITTED"

    def release(self, reservation_id: str) -> None:
        with self._lock:
            for r in self._items:
                if r.reservation_id == reservation_id and r.state == "RESERVED":
                    r.state = "RELEASED"

    def committed_total(self) -> int:
        with self._lock:
            return sum(r.amount.minor for r in self._items if r.state in ("RESERVED", "COMMITTED"))
