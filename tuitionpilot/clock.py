"""Deterministic simulation clock so every run of the sandbox is reproducible."""
from __future__ import annotations

from datetime import datetime, timedelta


class SimClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
        self._now = self._now + timedelta(days=days, hours=hours, minutes=minutes)
        return self._now
