"""Tamper-evident, append-only audit log (SHA-256 hash chain).

Every observation, decision, policy verdict and money movement is recorded.
Modifying or deleting any past entry breaks the chain and is detected by verify().
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, clock_now: Callable[[], datetime]):
        self._now = clock_now
        self.entries: list[dict] = []
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def record(self, actor: str, event: str, **data) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else GENESIS
        body = {
            "seq": len(self.entries),
            "ts": self._now().isoformat(),
            "actor": actor,          # agent | user | policy | guard | sandbox | system
            "event": event,
            "data": data,
            "prev_hash": prev,
        }
        body["hash"] = hashlib.sha256(_canon(body).encode()).hexdigest()
        self.entries.append(body)
        for fn in self._listeners:
            fn(body)
        return body

    def verify(self) -> tuple[bool, int | None]:
        """Return (ok, index_of_first_bad_entry)."""
        prev = GENESIS
        for i, e in enumerate(self.entries):
            body = {k: v for k, v in e.items() if k != "hash"}
            if body.get("prev_hash") != prev or body.get("seq") != i:
                return False, i
            if hashlib.sha256(_canon(body).encode()).hexdigest() != e["hash"]:
                return False, i
            prev = e["hash"]
        return True, None

    def to_jsonl(self) -> str:
        return "\n".join(_canon(e) for e in self.entries) + ("\n" if self.entries else "")

    def tail(self, n: int = 20) -> list[dict]:
        return self.entries[-n:]
