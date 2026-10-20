"""Money in integer minor units with per-currency precision, and directed rates.

No binary floats touch money. Every Amount knows its currency; adding two
different currencies raises. Every Rate states its direction explicitly:
``Rate(base="KZT", quote="CNY", value=0.0148)`` means 1 KZT = 0.0148 CNY.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

# ISO 4217 minor-unit exponents for the currencies the sandbox supports.
EXPONENT = {
    "KZT": 2, "CNY": 2, "USD": 2, "EUR": 2, "GBP": 2, "HKD": 2, "MYR": 2, "THB": 2,
    "INR": 2, "IDR": 2, "RUB": 2, "UZS": 2, "BRL": 2, "NGN": 2,
    "JPY": 0, "KRW": 0, "VND": 0,
}


class CurrencyError(ValueError):
    """Unknown currency, mixed currencies or an ambiguous pair."""


def exponent(ccy: str) -> int:
    if not isinstance(ccy, str) or ccy.upper() not in EXPONENT:
        raise CurrencyError(f"unknown or missing currency: {ccy!r}")
    return EXPONENT[ccy.upper()]


def to_decimal(x) -> Decimal:
    if isinstance(x, float):
        raise TypeError("binary floats are not accepted for money; pass str, int or Decimal")
    try:
        return Decimal(x) if not isinstance(x, Decimal) else x
    except InvalidOperation as e:
        raise ValueError(f"not a number: {x!r}") from e


@dataclass(frozen=True, order=True)
class Amount:
    minor: int
    ccy: str

    def __post_init__(self):
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError("minor units must be int")
        object.__setattr__(self, "ccy", self.ccy.upper())
        exponent(self.ccy)

    # ---- construction ------------------------------------------------------
    @classmethod
    def of(cls, major, ccy: str, rounding=ROUND_HALF_UP) -> "Amount":
        e = exponent(ccy)
        q = (to_decimal(major) * (Decimal(10) ** e)).quantize(Decimal(1), rounding=rounding)
        return cls(int(q), ccy)

    @classmethod
    def zero(cls, ccy: str) -> "Amount":
        return cls(0, ccy)

    # ---- views -------------------------------------------------------------
    @property
    def major(self) -> Decimal:
        return Decimal(self.minor).scaleb(-exponent(self.ccy))

    def __str__(self) -> str:
        e = exponent(self.ccy)
        return f"{self.major:,.{e}f} {self.ccy}"

    def to_dict(self) -> dict:
        return {"minor": self.minor, "ccy": self.ccy, "display": str(self)}

    # ---- arithmetic (same currency only) -------------------------------------
    def _same(self, other: "Amount"):
        if not isinstance(other, Amount):
            raise TypeError("can only combine Amount with Amount")
        if other.ccy != self.ccy:
            raise CurrencyError(f"cannot combine {self.ccy} and {other.ccy} without an explicit conversion")

    def __add__(self, other: "Amount") -> "Amount":
        self._same(other)
        return Amount(self.minor + other.minor, self.ccy)

    def __sub__(self, other: "Amount") -> "Amount":
        self._same(other)
        return Amount(self.minor - other.minor, self.ccy)

    def __neg__(self) -> "Amount":
        return Amount(-self.minor, self.ccy)

    def scale(self, factor, rounding=ROUND_HALF_UP) -> "Amount":
        """Multiply by a dimensionless factor (fee %, tranche share)."""
        q = (Decimal(self.minor) * to_decimal(factor)).quantize(Decimal(1), rounding=rounding)
        return Amount(int(q), self.ccy)

    def split(self, weights) -> list["Amount"]:
        """Split into parts proportional to weights; parts sum exactly to self."""
        ws = [to_decimal(w) for w in weights]
        tot = sum(ws)
        parts = [int((Decimal(self.minor) * w / tot).to_integral_value(rounding=ROUND_FLOOR)) for w in ws]
        parts[-1] += self.minor - sum(parts)
        return [Amount(p, self.ccy) for p in parts]


def amounts_sum(items, ccy: str) -> Amount:
    total = Amount.zero(ccy)
    for a in items:
        total = total + a
    return total


@dataclass(frozen=True)
class Rate:
    """1 unit of ``base`` = ``value`` units of ``quote``. Direction is explicit."""
    base: str
    quote: str
    value: Decimal

    def __post_init__(self):
        object.__setattr__(self, "base", self.base.upper())
        object.__setattr__(self, "quote", self.quote.upper())
        exponent(self.base); exponent(self.quote)
        if self.base == self.quote:
            raise CurrencyError("rate needs two different currencies")
        v = to_decimal(self.value)
        if v <= 0:
            raise ValueError("rate must be positive")
        object.__setattr__(self, "value", v)

    @property
    def label(self) -> str:
        return f"{self.quote} per 1 {self.base}"

    def invert(self) -> "Rate":
        return Rate(self.quote, self.base, Decimal(1) / self.value)

    def convert(self, amt: Amount, rounding=ROUND_FLOOR) -> Amount:
        """Convert an amount in ``base`` into ``quote``."""
        if amt.ccy != self.base:
            raise CurrencyError(f"rate is {self.label}; cannot convert {amt.ccy}")
        out = amt.major * self.value
        return Amount.of(out, self.quote, rounding=rounding)

    def source_needed(self, target: Amount) -> Amount:
        """Base-currency amount needed so that ``target`` (in quote ccy) is received. Rounded up."""
        if target.ccy != self.quote:
            raise CurrencyError(f"rate is {self.label}; target must be {self.quote}")
        return Amount.of(target.major / self.value, self.base, rounding=ROUND_CEILING)

    def to_dict(self) -> dict:
        return {"base": self.base, "quote": self.quote, "value": str(self.value), "label": self.label}


def mask_account(acc: str) -> str:
    s = "".join(ch for ch in str(acc) if ch.isalnum())
    return "•••• " + s[-4:] if s else ""
