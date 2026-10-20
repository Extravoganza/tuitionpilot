"""Core domain models. Money is always Decimal, never float."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

CENT = Decimal("0.01")


def D(x) -> Decimal:
    """Convert to Decimal safely (via str to avoid float artefacts)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q2(x: Decimal) -> Decimal:
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        object.__setattr__(self, "amount", q2(D(self.amount)))
        object.__setattr__(self, "currency", self.currency.upper())

    def __str__(self) -> str:
        return f"{self.amount:,.2f} {self.currency}"

    def to_dict(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}


class ObligationKind(str, Enum):
    TUITION = "tuition"
    RENT = "rent"
    INSURANCE = "insurance"
    VISA_FEE = "visa_fee"
    OTHER = "other"


@dataclass
class Payee:
    payee_id: str
    name: str
    account: str          # IBAN / account number at the receiving institution
    currency: str
    country: str
    verified: bool = False            # verified through an out-of-band channel
    added_at: Optional[datetime] = None
    category: ObligationKind = ObligationKind.OTHER

    def to_dict(self) -> dict:
        return {
            "payee_id": self.payee_id, "name": self.name, "account": self.account,
            "currency": self.currency, "country": self.country, "verified": self.verified,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "category": self.category.value,
        }


@dataclass
class Obligation:
    """Something the student must pay: tuition, rent, insurance..."""
    obligation_id: str
    kind: ObligationKind
    payee_id: str
    amount: Money
    due: datetime
    reference: str = ""
    paid: bool = False
    source: str = "manual"   # manual | invoice | recurring

    def to_dict(self) -> dict:
        return {
            "obligation_id": self.obligation_id, "kind": self.kind.value,
            "payee_id": self.payee_id, "amount": self.amount.to_dict(),
            "due": self.due.isoformat(), "reference": self.reference,
            "paid": self.paid, "source": self.source,
        }


@dataclass
class StudentProfile:
    name: str
    home_currency: str
    study_currency: str
    home_country: str
    study_country: str
    monthly_allowance: Money
    risk_preference: str = "balanced"     # cautious | balanced
    extra: dict = field(default_factory=dict)
