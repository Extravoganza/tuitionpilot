"""Parse tuition/rent invoices from plain text (email body, PDF text).

The parser extracts fields with strict patterns. It never executes or
"interprets" free text - that is what makes invoice content safe to read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

FIELD_PATTERNS = {
    "payee": r"^\s*(?:payee|beneficiary|pay to|university|landlord)\s*[:\-]\s*(.+)$",
    "account": r"^\s*(?:iban|account(?: number| no\.?)?|beneficiary account)\s*[:\-]\s*([A-Z0-9 \-]{6,40})\s*$",
    "amount": r"^\s*(?:amount(?: due)?|total(?: due)?|tuition fee)\s*[:\-]\s*([A-Z]{3})?\s*([0-9][0-9,\. ]*)\s*([A-Z]{3})?\s*$",
    "due": r"^\s*(?:due(?: date)?|deadline|pay by)\s*[:\-]\s*(\d{4}-\d{2}-\d{2})\s*$",
    "reference": r"^\s*(?:reference|ref|student id|payment reference)\s*[:\-]\s*([A-Za-z0-9\-/]{3,40})\s*$",
    "country": r"^\s*(?:country)\s*[:\-]\s*([A-Za-z]{2})\s*$",
}


@dataclass
class ParsedInvoice:
    payee: str | None = None
    account: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    due: datetime | None = None
    reference: str | None = None
    country: str | None = None
    missing: list | None = None

    def to_dict(self) -> dict:
        return {"payee": self.payee, "account": self.account,
                "amount": str(self.amount) if self.amount is not None else None,
                "currency": self.currency, "due": self.due.date().isoformat() if self.due else None,
                "reference": self.reference, "country": self.country, "missing": self.missing}


def parse_invoice(text: str) -> ParsedInvoice:
    inv = ParsedInvoice()
    for line in text.splitlines():
        for key, pat in FIELD_PATTERNS.items():
            m = re.match(pat, line, flags=re.IGNORECASE)
            if not m:
                continue
            if key == "amount" and inv.amount is None:
                ccy = (m.group(1) or m.group(3) or "").upper() or None
                raw = m.group(2).replace(",", "").replace(" ", "")
                try:
                    inv.amount = Decimal(raw)
                    inv.currency = ccy
                except InvalidOperation:
                    pass
            elif key == "due" and inv.due is None:
                inv.due = datetime.fromisoformat(m.group(1))
            elif key in ("payee", "account", "reference", "country") and getattr(inv, key) is None:
                setattr(inv, key, m.group(1).strip())
    inv.missing = [k for k in ("payee", "account", "amount", "currency", "due") if getattr(inv, k) in (None, "")]
    return inv
