"""Sandbox bank: multi-currency wallet, FX conversion and outbound transfers.

Nothing here touches real money. It is a deterministic ledger used to produce
reproducible execution evidence for the agent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .audit import AuditLog
from .clock import SimClock
from .market import FXMarket
from .models import D, Money, Payee, q2
from .rails import Quote

WALLET_FX_SPREAD = D("0.004")   # in-app multi-currency wallet spread (sandbox parameter)


class InsufficientFunds(Exception):
    pass


@dataclass
class Transfer:
    tx_id: str
    idempotency_key: str
    payee_id: str
    rail_id: str
    debit: Money
    credit: Money
    reference: str
    created: datetime
    arrives_by: datetime
    status: str = "pending"    # pending | settled | failed | cancelled

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id, "idempotency_key": self.idempotency_key,
            "payee_id": self.payee_id, "rail_id": self.rail_id,
            "debit": self.debit.to_dict(), "credit": self.credit.to_dict(),
            "reference": self.reference, "created": self.created.isoformat(),
            "arrives_by": self.arrives_by.isoformat(), "status": self.status,
        }


@dataclass
class SandboxBank:
    clock: SimClock
    market: FXMarket
    audit: AuditLog
    balances: dict = field(default_factory=dict)
    payees: dict = field(default_factory=dict)
    transfers: dict = field(default_factory=dict)
    conversions: list = field(default_factory=list)
    _idem: dict = field(default_factory=dict)

    # ---- accounts -------------------------------------------------------
    def deposit(self, m: Money, note: str = "deposit") -> None:
        self.balances[m.currency] = q2(self.balances.get(m.currency, D(0)) + m.amount)
        self.audit.record("sandbox", "deposit", amount=m.to_dict(), note=note)

    def balance(self, ccy: str) -> Money:
        return Money(self.balances.get(ccy.upper(), D(0)), ccy)

    def snapshot(self) -> dict:
        return {c: str(v) for c, v in sorted(self.balances.items())}

    def _debit(self, m: Money) -> None:
        have = self.balances.get(m.currency, D(0))
        if have < m.amount:
            raise InsufficientFunds(f"need {m}, have {have} {m.currency}")
        self.balances[m.currency] = q2(have - m.amount)

    # ---- payees ---------------------------------------------------------
    def add_payee(self, p: Payee) -> None:
        p.added_at = p.added_at or self.clock.now()
        self.payees[p.payee_id] = p
        self.audit.record("sandbox", "payee_added", payee=p.to_dict())

    # ---- FX -------------------------------------------------------------
    def quote_conversion(self, src: str, dst: str, amount_src: Decimal) -> dict:
        mid = self.market.mid(src, dst, self.clock.now())
        rate = mid * (1 - WALLET_FX_SPREAD)
        return {"src": src, "dst": dst, "amount_src": str(q2(amount_src)),
                "mid": str(mid), "rate": f"{rate:.8f}", "receive": str(q2(amount_src * rate))}

    def convert(self, src: str, dst: str, amount_src: Decimal, idempotency_key: str) -> dict:
        if idempotency_key in self._idem:
            self.audit.record("sandbox", "idempotent_replay", key=idempotency_key)
            return self._idem[idempotency_key]
        qt = self.quote_conversion(src, dst, amount_src)
        self._debit(Money(amount_src, src))
        recv = D(qt["receive"])
        self.balances[dst.upper()] = q2(self.balances.get(dst.upper(), D(0)) + recv)
        rec = {**qt, "ts": self.clock.now().isoformat(), "key": idempotency_key}
        self.conversions.append(rec)
        self._idem[idempotency_key] = rec
        self.audit.record("sandbox", "fx_converted", **rec, balances=self.snapshot())
        return rec

    # ---- transfers ------------------------------------------------------
    def send(self, payee_id: str, qt: Quote, reference: str, idempotency_key: str) -> Transfer:
        if idempotency_key in self._idem:
            self.audit.record("sandbox", "idempotent_replay", key=idempotency_key)
            return self._idem[idempotency_key]
        if payee_id not in self.payees:
            raise KeyError("unknown payee")
        self._debit(qt.send)
        tx_id = "TX-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12].upper()
        tx = Transfer(tx_id, idempotency_key, payee_id, qt.rail_id, qt.send, qt.receive,
                      reference, self.clock.now(), qt.arrives_by)
        self.transfers[tx_id] = tx
        self._idem[idempotency_key] = tx
        self.audit.record("sandbox", "transfer_submitted", tx=tx.to_dict(), balances=self.snapshot())
        return tx

    def settle_due(self) -> list[Transfer]:
        done = []
        for tx in self.transfers.values():
            if tx.status == "pending" and tx.arrives_by <= self.clock.now():
                tx.status = "settled"
                done.append(tx)
                self.audit.record("sandbox", "transfer_settled", tx_id=tx.tx_id,
                                  credit=tx.credit.to_dict(), payee_id=tx.payee_id)
        return done
