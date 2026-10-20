"""Decision ledger: append-only events with a SHA-256 hash chain.

    H_n = SHA-256( canonical_json(event_n without "event_hash") || H_(n-1) )

canonical_json = UTF-8 JSON, keys sorted, separators (",", ":"), ensure_ascii=False.
Genesis H_0 = 64 × "0". Sequence numbers start at 1.

What this detects: any edit or deletion of an event *inside* the chain.
What it does NOT detect on its own: deletion of the tail, or a complete
rewrite by someone who controls the storage. For that, export checkpoints
(sequence + head hash) to an independent place and compare later
(``verify_against_checkpoint``). Tamper-evident is not the same as legally
non-repudiable.

Payloads are redacted before hashing: secrets (tokens, OTPs, passwords) are
removed and account numbers are masked. Raw approval tokens never enter the log.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

GENESIS = "0" * 64
POLICY_VERSION = "2.0.0"
APP_VERSION = "0.2.0"
ENVIRONMENT = "SIMULATION"

SECRET_KEYS = re.compile(r"(token|otp|password|secret|api_key|credential|passport)", re.I)
ACCOUNT_KEYS = re.compile(r"(account|iban|acct)", re.I)


class AuditUnavailable(RuntimeError):
    """The ledger cannot accept writes. Money-moving actions must fail closed."""


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _mask(v: str) -> str:
    s = "".join(ch for ch in str(v) if ch.isalnum())
    return "•••• " + s[-4:] if s else ""


def redact(payload):
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if SECRET_KEYS.search(str(k)) and not str(k).endswith("_hash_prefix"):
                out[k] = "[REDACTED]"
            elif ACCOUNT_KEYS.search(str(k)) and isinstance(v, str):
                out[k] = _mask(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(payload, list):
        return [redact(x) for x in payload]
    return payload


@dataclass(frozen=True)
class Checkpoint:
    sequence: int
    head_hash: str
    taken_at: str


class Ledger:
    def __init__(self, now: Callable[[], datetime], correlation_id: str | None = None):
        self._now = now
        self.events: list[dict] = []
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.available = True          # set False to simulate an outage
        self._ids = 0

    def _next_id(self) -> str:
        self._ids += 1
        return f"EVT-{self._ids:05d}"

    def append(self, actor: str, event_type: str, entity: str = "", payload: dict | None = None) -> dict:
        if not self.available:
            raise AuditUnavailable("audit store unavailable")
        red = redact(payload or {})
        prev = self.events[-1]["event_hash"] if self.events else GENESIS
        ev = {
            "event_id": self._next_id(),
            "sequence": len(self.events) + 1,
            "timestamp": self._now().astimezone(timezone.utc).isoformat(),
            "actor": actor, "event_type": event_type, "entity": entity,
            "payload": red,
            "payload_hash": hashlib.sha256(canonical_json(red).encode()).hexdigest(),
            "prev_hash": prev,
            "policy_version": POLICY_VERSION, "app_version": APP_VERSION,
            "correlation_id": self.correlation_id, "environment": ENVIRONMENT,
        }
        ev["event_hash"] = hashlib.sha256(canonical_json(ev).encode() + prev.encode()).hexdigest()
        self.events.append(ev)
        return ev

    # ---- verification ------------------------------------------------------
    def verify(self) -> dict:
        prev = GENESIS
        for i, ev in enumerate(self.events):
            body = {k: v for k, v in ev.items() if k != "event_hash"}
            if body.get("sequence") != i + 1:
                return {"ok": False, "first_bad_sequence": i + 1, "reason": "sequence gap or reorder"}
            if body.get("prev_hash") != prev:
                return {"ok": False, "first_bad_sequence": i + 1, "reason": "previous-hash link broken"}
            if hashlib.sha256(canonical_json(body["payload"]).encode()).hexdigest() != body["payload_hash"]:
                return {"ok": False, "first_bad_sequence": i + 1, "reason": "payload hash mismatch"}
            if hashlib.sha256(canonical_json(body).encode() + prev.encode()).hexdigest() != ev["event_hash"]:
                return {"ok": False, "first_bad_sequence": i + 1, "reason": "event hash mismatch"}
            prev = ev["event_hash"]
        return {"ok": True, "events": len(self.events), "head_hash": prev}

    def checkpoint(self) -> Checkpoint:
        head = self.events[-1]["event_hash"] if self.events else GENESIS
        return Checkpoint(len(self.events), head, self._now().astimezone(timezone.utc).isoformat())

    def verify_against_checkpoint(self, cp: Checkpoint) -> dict:
        base = self.verify()
        if not base["ok"]:
            return base
        if len(self.events) < cp.sequence:
            return {"ok": False, "reason": f"ledger has {len(self.events)} events, checkpoint saw {cp.sequence}: tail deleted"}
        if cp.sequence and self.events[cp.sequence - 1]["event_hash"] != cp.head_hash:
            return {"ok": False, "reason": "event at checkpoint sequence differs: history rewritten"}
        return {"ok": True, "checked_up_to": cp.sequence}

    # ---- export ------------------------------------------------------------
    def to_jsonl(self) -> str:
        return "".join(canonical_json(e) + "\n" for e in self.events)

    def to_csv(self) -> str:
        buf = io.StringIO()
        cols = ["sequence", "event_id", "timestamp", "actor", "event_type", "entity", "payload_hash",
                "prev_hash", "event_hash", "policy_version", "environment"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for e in self.events:
            w.writerow(e)
        return buf.getvalue()

    def find(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == event_type]
