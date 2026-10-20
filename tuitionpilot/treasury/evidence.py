"""Evidence Pack (spec v2.1 §16).

A pack is a manifest of named items, each with the SHA-256 of its canonical
JSON, plus the ledger head and verification result. ``verify_pack`` reports
VALID, INVALID (an item changed) or INCOMPLETE (an item is missing).

Limits, stated in every pack: a hash proves the item matches the manifest,
not that the source document is authentic and not that money was credited
externally. The hash chain detects edits relative to a trusted checkpoint; it
does not make the log immutable for someone who controls data and hashes.

PII is redacted by default and approval tokens are never exported: building a
pack that would contain a raw token fails.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .ledger import Ledger, canonical_json, redact

SCHEMA_VERSION = "1.0"
LIMITATIONS = [
    "Hashes prove the items match this manifest, not that the source documents are authentic.",
    "Sandbox events are not bank confirmations; no real money moved.",
    "The hash chain detects changes relative to a trusted checkpoint; it is tamper-evident, not tamper-proof.",
]
_TOKEN_KEY = re.compile(r"(^|_)(token|otp|secret|password)($|_)", re.I)


class ExportBlocked(RuntimeError):
    pass


def _find_secrets(obj, path="") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if _TOKEN_KEY.search(str(k)) and isinstance(v, str) and v not in ("[REDACTED]", ""):
                hits.append(p)
            hits += _find_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _find_secrets(v, f"{path}[{i}]")
    return hits


def _h(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def build_pack(items: dict, ledger: Ledger, created_at: datetime, redact_pii: bool = True) -> dict:
    leaks = _find_secrets(items)
    if leaks:
        raise ExportBlocked(f"export would contain secrets at: {', '.join(leaks)}")
    body = {k: (redact(v) if redact_pii else v) for k, v in items.items()}
    ver = ledger.verify()
    manifest = {"schema_version": SCHEMA_VERSION, "created_at": created_at.isoformat(),
                "environment": "SIMULATION", "label": "SIMULATION — NO REAL MONEY MOVES",
                "redaction": "PII redacted by default" if redact_pii else "none",
                "items": [{"name": k, "sha256": _h(v)} for k, v in sorted(body.items())],
                "ledger": {"events": len(ledger.events), "head_hash": ver.get("head_hash"), "chain_ok": ver["ok"]},
                "limitations": LIMITATIONS}
    manifest["manifest_id"] = "EP-" + _h(manifest)[:12]
    return {"manifest": manifest, "items": body}


def verify_pack(pack: dict) -> dict:
    m, items = pack["manifest"], pack["items"]
    problems = []
    for entry in m["items"]:
        if entry["name"] not in items:
            problems.append({"item": entry["name"], "code": "INTEGRITY_INCOMPLETE"})
        elif _h(items[entry["name"]]) != entry["sha256"]:
            problems.append({"item": entry["name"], "code": "INTEGRITY_INVALID"})
    extra = sorted(set(items) - {e["name"] for e in m["items"]})
    for x in extra:
        problems.append({"item": x, "code": "INTEGRITY_INVALID"})
    body = {k: v for k, v in m.items() if k != "manifest_id"}
    if "EP-" + _h(body)[:12] != m.get("manifest_id"):
        problems.append({"item": "manifest", "code": "INTEGRITY_INVALID"})
    if any(p["code"] == "INTEGRITY_INVALID" for p in problems):
        status = "INVALID"
    elif problems:
        status = "INCOMPLETE"
    else:
        status = "VALID"
    return {"status": status, "problems": problems, "manifest_id": m.get("manifest_id"), "items": len(m["items"])}
