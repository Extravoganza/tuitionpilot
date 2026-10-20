"""Payment tracking and reconciliation (spec v2.1 §13).

Submitted, processing, sent and credited are different states. The sandbox
executor accepting an intent is SUBMITTED_SIMULATED, never "paid". An
obligation counts as settled only after a separate CREDITED_SIMULATED event
that reconciles exactly (amount, currency, beneficiary version, reference,
date window). Fuzzy matches are candidates for a human, never auto-closed.
A sandbox event is not an external bank confirmation.

Also here: the sandbox provider's idempotency store. Same key + same payload
returns the existing record; same key + a different payload is
IDEMPOTENCY_CONFLICT.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .currency import Amount
from .ledger import Ledger, canonical_json

SIM = "SIMULATION — NO REAL MONEY MOVES"

STATUSES = ("DRAFT", "NEEDS_REVIEW", "READY_FOR_APPROVAL", "APPROVED", "SUBMITTED_SIMULATED",
            "PROCESSING_SIMULATED", "SENT_SIMULATED", "CREDIT_PENDING", "CREDITED_SIMULATED",
            "RECONCILIATION_REQUIRED", "FAILED_SIMULATED", "CANCELLED")

ALLOWED = {
    "DRAFT": {"NEEDS_REVIEW", "READY_FOR_APPROVAL", "CANCELLED"},
    "NEEDS_REVIEW": {"READY_FOR_APPROVAL", "CANCELLED"},
    "READY_FOR_APPROVAL": {"APPROVED", "NEEDS_REVIEW", "CANCELLED"},
    "APPROVED": {"SUBMITTED_SIMULATED", "CANCELLED"},
    "SUBMITTED_SIMULATED": {"PROCESSING_SIMULATED", "FAILED_SIMULATED"},
    "PROCESSING_SIMULATED": {"SENT_SIMULATED", "FAILED_SIMULATED"},
    "SENT_SIMULATED": {"CREDIT_PENDING", "CREDITED_SIMULATED", "RECONCILIATION_REQUIRED", "FAILED_SIMULATED"},
    "CREDIT_PENDING": {"CREDITED_SIMULATED", "RECONCILIATION_REQUIRED", "FAILED_SIMULATED"},
    "CREDITED_SIMULATED": {"RECONCILIATION_REQUIRED"},
    "RECONCILIATION_REQUIRED": {"CREDITED_SIMULATED"},
    "FAILED_SIMULATED": set(),
    "CANCELLED": set(),
}


class IllegalTransition(ValueError):
    pass


class IdempotencyConflict(RuntimeError):
    code = "IDEMPOTENCY_CONFLICT"


@dataclass
class Expected:
    amount: Amount
    beneficiary_version_id: str
    reference: str
    window: tuple                   # (earliest date, latest date)


@dataclass
class Payment:
    payment_id: str
    intent_id: str
    expected: Expected
    status: str = "DRAFT"
    timeline: list = field(default_factory=list)
    reconciliation: Optional[dict] = None


class PaymentTracker:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.payments: dict[str, Payment] = {}
        self._n = 0

    def create(self, intent_id: str, expected: Expected, at: datetime) -> Payment:
        self._n += 1
        p = Payment(f"PAY-{self._n:04d}", intent_id, expected)
        self.payments[p.payment_id] = p
        self._event(p, "DRAFT", at, "created from plan", "SANDBOX")
        return p

    def _event(self, p: Payment, status: str, at: datetime, note: str, source: str, data: Optional[dict] = None):
        ev = {"status": status, "at": at.isoformat(), "note": note, "source": source, "label": SIM, **(data or {})}
        p.timeline.append(ev)
        self.ledger.append("tracker", "payment_status", p.payment_id, {"status": status, "source": source, "note": note})
        return ev

    def advance(self, payment_id: str, status: str, at: datetime, note: str = "", source: str = "SANDBOX",
                data: Optional[dict] = None) -> Payment:
        p = self.payments[payment_id]
        if status not in ALLOWED[p.status]:
            raise IllegalTransition(f"{p.status} → {status} is not allowed")
        p.status = status
        self._event(p, status, at, note, source, data)
        return p

    @staticmethod
    def obligation_settled(p: Payment) -> bool:
        """Only a reconciled credit closes the obligation. Submitted is not credited."""
        return p.status == "CREDITED_SIMULATED" and bool(p.reconciliation) and p.reconciliation["status"] == "RECONCILED"

    # ------------------------------------------------------- reconciliation
    def reconcile(self, payment_id: str, credits: list[dict], at: datetime) -> dict:
        """credits: simulated credit events {amount: Amount, beneficiary_version_id, reference, date, source}."""
        p = self.payments[payment_id]
        e = p.expected
        if not credits:
            res = {"status": "CREDIT_PENDING", "reason_codes": [], "candidates": [],
                   "note": "no credit event yet: the payment is submitted, not credited"}
            if p.status == "SENT_SIMULATED":
                self.advance(payment_id, "CREDIT_PENDING", at, "waiting for credit confirmation")
            p.reconciliation = res
            return res
        exact, fuzzy = [], []
        for c in credits:
            diffs = []
            if c["amount"].ccy != e.amount.ccy:
                diffs.append("CURRENCY_MISMATCH")
            elif c["amount"].minor != e.amount.minor:
                diffs.append("AMOUNT_MISMATCH")
            if c.get("beneficiary_version_id") != e.beneficiary_version_id:
                diffs.append("BENEFICIARY_CHANGED")
            if c.get("reference") != e.reference:
                diffs.append("REFERENCE_MISMATCH")
            d = c.get("date")
            if d is None or not (e.window[0] <= d <= e.window[1]):
                diffs.append("DATE_OUTSIDE_WINDOW")
            (exact if not diffs else fuzzy).append({"credit": {k: str(v) for k, v in c.items()}, "differences": diffs})
        if len(exact) == 1:
            res = {"status": "RECONCILED", "reason_codes": [], "matched": exact[0], "candidates": []}
            if p.status in ("SENT_SIMULATED", "CREDIT_PENDING", "RECONCILIATION_REQUIRED"):
                self.advance(payment_id, "CREDITED_SIMULATED", at, "exact match on amount, currency, payee, reference, window")
        else:
            codes = sorted({x for f in fuzzy for x in f["differences"]}) or ["DUPLICATE_INTENT"]
            res = {"status": "RECONCILIATION_REQUIRED", "reason_codes": codes, "candidates": fuzzy or exact,
                   "note": "candidates need a person; nothing is closed automatically"}
            if p.status in ("SENT_SIMULATED", "CREDIT_PENDING", "CREDITED_SIMULATED"):
                self.advance(payment_id, "RECONCILIATION_REQUIRED", at, ", ".join(codes))
        p.reconciliation = res
        return res

    def resolve_manually(self, payment_id: str, actor: str, reason: str, evidence: str, at: datetime) -> dict:
        p = self.payments[payment_id]
        if p.status != "RECONCILIATION_REQUIRED":
            raise IllegalTransition("nothing to resolve")
        if not (actor and reason and evidence):
            raise ValueError("manual resolution needs actor, reason and evidence")
        p.reconciliation = {**(p.reconciliation or {}), "status": "RECONCILED", "manual": True,
                            "resolved_by": actor, "reason": reason, "evidence": evidence, "resolved_at": at.isoformat()}
        self.advance(payment_id, "CREDITED_SIMULATED", at, f"resolved by {actor}: {reason}", "MANUAL")
        return p.reconciliation


# ----------------------------------------------------- provider idempotency
def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


class SandboxProvider:
    """A fake provider endpoint with a unique idempotency key constraint."""

    def __init__(self):
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {}
        self.executions = 0

    def submit(self, key: str, payload: dict) -> dict:
        h = payload_hash(payload)
        with self._lock:
            if key in self.records:
                rec = self.records[key]
                if rec["payload_hash"] != h:
                    raise IdempotencyConflict(f"key {key} reused with a different payload")
                return {**rec, "replayed": True}
            self.executions += 1
            rec = {"provider_ref": f"SBX-{self.executions:04d}", "payload_hash": h, "state": "SUBMITTED_SIMULATED",
                   "label": SIM}
            self.records[key] = rec
            return {**rec, "replayed": False}
