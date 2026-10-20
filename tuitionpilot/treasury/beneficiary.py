"""Tuition Shield: immutable beneficiary versions, verification evidence, field-level diff.

An invoice is never the source of truth. A beneficiary version becomes
payable only after independent verification through a trusted channel taken
from an existing source (the university website, a previous payment), never
from the invoice or email being checked. Any change to the payment fields
creates a new immutable version that starts unverified and on a 48-hour hold.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .currency import mask_account

PAYMENT_FIELDS = ("legal_name", "account", "bank_bic", "country", "currency")
HOLD = timedelta(hours=48)
TRUSTED_METHODS = {"trusted_callback", "prior_payment"}
UNTRUSTED_SOURCES = {"invoice", "email", "invoice_pdf", "sender_reply"}


@dataclass(frozen=True)
class BeneficiaryVersion:
    beneficiary_id: str
    version: int
    legal_name: str
    account: str
    bank_bic: str
    country: str
    currency: str
    created_at: datetime
    origin: str                     # onboarding | invoice | user_edit

    @property
    def version_id(self) -> str:
        blob = json.dumps({f: getattr(self, f) for f in ("beneficiary_id", "version") + PAYMENT_FIELDS},
                          sort_keys=True)
        return "BV-" + hashlib.sha256(blob.encode()).hexdigest()[:12]

    def fields(self) -> dict:
        return {f: getattr(self, f) for f in PAYMENT_FIELDS}

    def masked(self) -> dict:
        d = self.fields()
        d["account"] = mask_account(d["account"])
        return {"version_id": self.version_id, "version": self.version, **d,
                "created_at": self.created_at.isoformat(), "origin": self.origin}


@dataclass(frozen=True)
class Verification:
    version_id: str
    method: str
    contact_source: str
    verified_by: str
    verified_at: datetime


@dataclass(frozen=True)
class FieldDiff:
    field: str
    invoice_value: str
    verified_value: str
    match: bool

    def to_dict(self) -> dict:
        mask = (lambda v: mask_account(v)) if self.field == "account" else (lambda v: v)
        return {"field": self.field, "invoice": mask(self.invoice_value), "verified": mask(self.verified_value),
                "match": self.match}


def _norm(field_name: str, v: str) -> str:
    s = str(v or "").strip().upper()
    if field_name in ("account", "bank_bic"):
        s = "".join(ch for ch in s if ch.isalnum())
    return " ".join(s.split())


class VerificationRejected(ValueError):
    pass


class BeneficiaryRegistry:
    def __init__(self):
        self.versions: dict[str, list[BeneficiaryVersion]] = {}
        self.verifications: dict[str, Verification] = {}

    def add_version(self, beneficiary_id: str, fields: dict, created_at: datetime, origin: str) -> BeneficiaryVersion:
        hist = self.versions.setdefault(beneficiary_id, [])
        v = BeneficiaryVersion(beneficiary_id, len(hist) + 1, *(str(fields[f]) for f in PAYMENT_FIELDS),
                               created_at=created_at, origin=origin)
        hist.append(v)
        return v

    def verify(self, version_id: str, method: str, contact_source: str, verified_by: str, at: datetime) -> Verification:
        if method not in TRUSTED_METHODS:
            raise VerificationRejected(f"method {method!r} is not an independent verification")
        if contact_source.lower() in UNTRUSTED_SOURCES:
            raise VerificationRejected("contact details must come from an existing trusted source, not the invoice")
        if not any(v.version_id == version_id for vs in self.versions.values() for v in vs):
            raise VerificationRejected("unknown beneficiary version")
        rec = Verification(version_id, method, contact_source, verified_by, at)
        self.verifications[version_id] = rec
        return rec

    def history(self, beneficiary_id: str) -> list[BeneficiaryVersion]:
        return list(self.versions.get(beneficiary_id, []))

    def latest_verified(self, beneficiary_id: str) -> BeneficiaryVersion | None:
        for v in reversed(self.versions.get(beneficiary_id, [])):
            if v.version_id in self.verifications:
                return v
        return None

    def get(self, version_id: str) -> BeneficiaryVersion | None:
        for vs in self.versions.values():
            for v in vs:
                if v.version_id == version_id:
                    return v
        return None

    def diff(self, beneficiary_id: str, invoice_fields: dict) -> list[FieldDiff]:
        ver = self.latest_verified(beneficiary_id)
        base = ver.fields() if ver else {f: "" for f in PAYMENT_FIELDS}
        out = []
        for f in PAYMENT_FIELDS:
            iv = str(invoice_fields.get(f, "") or "")
            out.append(FieldDiff(f, iv, base[f], bool(iv) and _norm(f, iv) == _norm(f, base[f])))
        return out

    def payable_status(self, version_id: str, now: datetime) -> tuple[str, list[str]]:
        """VERIFIED | HOLD | UNVERIFIED | UNKNOWN, with reason codes."""
        v = self.get(version_id)
        if v is None:
            return "UNKNOWN", ["BENEFICIARY_UNKNOWN"]
        if version_id in self.verifications:
            return "VERIFIED", []
        reasons = ["BENEFICIARY_NOT_INDEPENDENTLY_VERIFIED"]
        if now - v.created_at < HOLD:
            return "HOLD", reasons + ["NEW_BENEFICIARY_48H_HOLD"]
        return "UNVERIFIED", reasons

    def check_invoice(self, beneficiary_id: str, invoice_fields: dict) -> dict:
        diffs = self.diff(beneficiary_id, invoice_fields)
        ver = self.latest_verified(beneficiary_id)
        mism = [d.field for d in diffs if not d.match]
        if ver is None:
            status = "NO_VERIFIED_VERSION"
        elif mism:
            status = "MISMATCH_BLOCKED"
        else:
            status = "MATCH_VERIFIED"
        return {"status": status, "verified_version": ver.masked() if ver else None,
                "mismatched_fields": mism, "diff": [d.to_dict() for d in diffs],
                "note": "Extraction from an invoice never verifies a payee; only an independent callback does."}
