"""Payment intents, exact-action approval challenges and the sandbox executor's records.

An intent's fingerprint covers every field that matters to the student:
intent, obligation, source amount, target amount, currency pair, beneficiary
version, route, quote, execution date and policy version. Approvals bind to
that fingerprint; any change produces a different fingerprint and the
approval no longer redeems (POL-021). Only the SHA-256 of the approval token
is stored; the raw token is returned once, to the user channel.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from .currency import Amount
from .ledger import POLICY_VERSION, canonical_json


@dataclass(frozen=True)
class IntentSpec:
    intent_id: str
    user_id: str
    obligation_id: str
    source_amount: Amount
    target_amount: Amount
    beneficiary_version_id: str
    route_id: str
    quote_id: str
    execution_date: date
    invoice_id: str = ""
    policy_version: str = POLICY_VERSION

    @property
    def currency_pair(self) -> tuple:
        return (self.source_amount.ccy, self.target_amount.ccy)

    def fingerprint(self) -> str:
        body = {"intent_id": self.intent_id, "user_id": self.user_id, "obligation_id": self.obligation_id,
                "source_minor": self.source_amount.minor, "source_ccy": self.source_amount.ccy,
                "target_minor": self.target_amount.minor, "target_ccy": self.target_amount.ccy,
                "beneficiary_version_id": self.beneficiary_version_id, "route_id": self.route_id,
                "quote_id": self.quote_id, "execution_date": self.execution_date.isoformat(),
                "invoice_id": self.invoice_id, "policy_version": self.policy_version}
        return hashlib.sha256(canonical_json(body).encode()).hexdigest()

    def summary(self) -> dict:
        return {"intent_id": self.intent_id, "obligation_id": self.obligation_id,
                "source_amount": str(self.source_amount), "target_amount": str(self.target_amount),
                "currency_pair": "/".join(self.currency_pair), "beneficiary_version_id": self.beneficiary_version_id,
                "route_id": self.route_id, "quote_id": self.quote_id, "execution_date": self.execution_date.isoformat(),
                "policy_version": self.policy_version, "fingerprint": self.fingerprint()[:16]}


@dataclass
class ApprovalChallenge:
    approval_id: str
    fingerprint: str
    token_hash: str
    created_at: datetime
    expires_at: datetime
    status: str = "PENDING"          # PENDING | USED | EXPIRED | INVALIDATED

    def public(self) -> dict:
        return {"approval_id": self.approval_id, "fingerprint": self.fingerprint[:16], "status": self.status,
                "expires_at": self.expires_at.isoformat(), "token_hash_prefix": self.token_hash[:8]}


class ApprovalService:
    def __init__(self, ttl: timedelta = timedelta(minutes=15), token_source: Callable[[], str] | None = None):
        self.ttl = ttl
        self._token = token_source or (lambda: secrets.token_urlsafe(16))
        self._lock = threading.Lock()
        self.challenges: dict[str, ApprovalChallenge] = {}
        self._n = 0

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, spec: IntentSpec, now: datetime) -> tuple[ApprovalChallenge, str]:
        with self._lock:
            self._n += 1
            raw = self._token()
            ch = ApprovalChallenge(f"APR-{self._n:04d}", spec.fingerprint(), self._h(raw), now, now + self.ttl)
            self.challenges[ch.approval_id] = ch
            return ch, raw

    def redeem(self, raw_token: str, spec: IntentSpec, now: datetime) -> tuple[str, ApprovalChallenge | None]:
        """Returns ("VALID", ch) or ("INVALID:<REASON>", ch|None). One-time."""
        h = self._h(raw_token or "")
        with self._lock:
            ch = next((c for c in self.challenges.values() if c.token_hash == h), None)
            if ch is None:
                return "INVALID:UNKNOWN_TOKEN", None
            if ch.status == "USED":
                return "INVALID:REUSED", ch
            if ch.status == "INVALIDATED":
                return "INVALID:INVALIDATED", ch
            if now > ch.expires_at:
                ch.status = "EXPIRED"
                return "INVALID:EXPIRED", ch
            if ch.fingerprint != spec.fingerprint():
                ch.status = "INVALIDATED"
                return "INVALID:FINGERPRINT_MISMATCH", ch
            ch.status = "USED"
            return "VALID", ch


@dataclass
class ExecutionAttempt:
    execution_id: str
    intent_id: str
    idempotency_key: str
    state: str                       # EXECUTING | SIMULATED_SUBMITTED | FAILED
    reservation_id: str | None = None
    attempts: int = 1
    environment: str = "SIMULATION"

    def to_dict(self) -> dict:
        return {"execution_id": self.execution_id, "intent_id": self.intent_id, "state": self.state,
                "attempts": self.attempts, "environment": self.environment,
                "label": "SIMULATION — NO REAL MONEY MOVES"}


class IntentStore:
    """Unique idempotency keys. In-process lock stands in for a DB unique constraint (ADR-001)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.by_key: dict[str, ExecutionAttempt] = {}
        self._n = 0

    def get(self, key: str) -> ExecutionAttempt | None:
        with self._lock:
            return self.by_key.get(key)

    def create(self, key: str, intent_id: str) -> tuple[ExecutionAttempt, bool]:
        """Returns (attempt, created). Never creates a second attempt for a key."""
        with self._lock:
            if key in self.by_key:
                a = self.by_key[key]
                a.attempts += 1
                return a, False
            self._n += 1
            a = ExecutionAttempt(f"EXE-{self._n:04d}", intent_id, key, "EXECUTING")
            self.by_key[key] = a
            return a, True
