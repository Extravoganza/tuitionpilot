"""Business-day calendar: weekends, holidays, bank cut-off and time zones.

Due date and latest safe arrival are different things. ``latest_safe_arrival``
subtracts the university's processing buffer in business days from the due
date. Arrival ranges are computed from the *effective* execution date: an
instruction after the bank's cut-off, or on a non-business day, rolls to the
next business day. Conservative feasibility always uses the late end of the
arrival range.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BusinessCalendar:
    tz: str = "Asia/Almaty"
    cutoff: time = time(15, 0)
    holidays: frozenset = field(default_factory=frozenset)   # TBD per corridor
    weekend: frozenset = frozenset({5, 6})                  # Sat, Sun

    def is_business_day(self, d: date) -> bool:
        return d.weekday() not in self.weekend and d not in self.holidays

    def next_business_day(self, d: date) -> date:
        d = d + timedelta(days=1)
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d

    def roll_forward(self, d: date) -> date:
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d

    def add_business_days(self, d: date, n: int) -> date:
        if n < 0:
            return self.subtract_business_days(d, -n)
        cur = d
        for _ in range(n):
            cur = self.next_business_day(cur)
        return cur

    def subtract_business_days(self, d: date, n: int) -> date:
        cur = d
        for _ in range(n):
            cur -= timedelta(days=1)
            while not self.is_business_day(cur):
                cur -= timedelta(days=1)
        return cur

    def business_days_between(self, a: date, b: date) -> int:
        """Signed count of business days from a (exclusive) to b (inclusive)."""
        if a == b:
            return 0
        sign, lo, hi = (1, a, b) if b > a else (-1, b, a)
        n, cur = 0, lo
        while cur < hi:
            cur += timedelta(days=1)
            if self.is_business_day(cur):
                n += 1
        return sign * n

    def effective_execution_date(self, instructed_at: datetime) -> date:
        """Local date on which the bank actually processes an instruction."""
        if instructed_at.tzinfo is None:
            raise ValueError("instruction time must be timezone-aware")
        local = instructed_at.astimezone(ZoneInfo(self.tz))
        d = local.date()
        if not self.is_business_day(d):
            return self.roll_forward(d)
        if local.time() >= self.cutoff:
            return self.next_business_day(d)
        return d

    def arrival_range(self, execution_date: date, min_bd: int, max_bd: int) -> tuple[date, date]:
        return self.add_business_days(execution_date, min_bd), self.add_business_days(execution_date, max_bd)


def latest_safe_arrival(due: date, processing_buffer_bd: int, cal: BusinessCalendar) -> date:
    return cal.subtract_business_days(cal.roll_forward(due) if not cal.is_business_day(due) else due,
                                      processing_buffer_bd)


def instruction_time_for(d: date, cal: BusinessCalendar, at: time = time(10, 0)) -> datetime:
    """A timezone-aware instruction timestamp on local date d (before cut-off)."""
    return datetime.combine(d, at, tzinfo=ZoneInfo(cal.tz))
