"""Deterministic policy engine, rule IDs POL-001…POL-023 (workbook Policy_Rules + one addition).

Input: a fully populated ``PolicyInput``. Output: ``PolicyResult`` with
decision ALLOW | ASK | DENY | RETURN_EXISTING, reason codes, matched rule IDs,
required approval, expiry and policy version.

Fail closed: if any field needed for a money-moving decision is missing,
the answer is DENY (POL-020), never a default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from .currency import Amount
from .ledger import POLICY_VERSION
from .limits import Caps

MONEY_MOVING = {"EXECUTE_PAYMENT", "FX_BUY"}


class Decision(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"
    RETURN_EXISTING = "RETURN_EXISTING"


@dataclass
class PolicyInput:
    action: str                                   # READ | PROPOSE | FX_BUY | EXECUTE_PAYMENT | CHANGE_POLICY
    actor: str = "agent"                          # agent | user
    user_id: str | None = None                    # owner of the data/intent
    session_user_id: str | None = None            # authenticated caller
    tier: int | None = None
    amount: Amount | None = None                  # source-currency amount, all-in
    target: Amount | None = None
    currency_pair: tuple | None = None            # (source, target), explicit
    quote_fresh: bool | None = None
    beneficiary_status: str | None = None         # VERIFIED | HOLD | UNVERIFIED | UNKNOWN | MISMATCH
    liquidity_ok: bool | None = None
    kill_switch: bool | None = None
    audit_available: bool | None = None
    invoice_flags: list = field(default_factory=list)
    fx_share_pct: Decimal | None = None
    cap_reasons: list = field(default_factory=list)   # from a dry or atomic cap check
    duplicate_of: str | None = None
    approval: str = "MISSING"                     # VALID | MISSING | INVALID:<REASON>
    recheck: bool = False                         # evaluation at redemption / execution


@dataclass
class PolicyResult:
    decision: Decision
    reason_codes: list
    matched_rule_ids: list
    required_approval: dict | None = None
    expires_at: str | None = None
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict:
        return {"decision": self.decision.value, "reason_codes": self.reason_codes,
                "matched_rule_ids": self.matched_rule_ids, "required_approval": self.required_approval,
                "expires_at": self.expires_at, "policy_version": self.policy_version}


class PolicyEngine:
    def __init__(self, caps: Caps, approval_ttl: timedelta = timedelta(minutes=15)):
        self.caps = caps
        self.approval_ttl = approval_ttl

    def evaluate(self, p: PolicyInput, now: datetime) -> PolicyResult:
        rules, reasons = [], []

        def out(dec: Decision, **kw) -> PolicyResult:
            return PolicyResult(dec, reasons, rules, **kw)

        def deny(rule: str, code: str) -> PolicyResult:
            rules.append(rule); reasons.append(code)
            return out(Decision.DENY)

        money = p.action in MONEY_MOVING

        # --- identity and protected state -------------------------------------------------
        if p.user_id is None or p.session_user_id is None or p.user_id != p.session_user_id:
            return deny("POL-001", "IDENTITY_MISSING_OR_SCOPE_MISMATCH")
        if p.action == "CHANGE_POLICY":
            return deny("POL-023", "POLICY_MUTATION_ONLY_BY_AUTHENTICATED_USER_OUTSIDE_AGENT")
        if p.action not in MONEY_MOVING | {"READ", "PROPOSE"}:
            return deny("POL-020", "UNKNOWN_ACTION")
        if p.action == "READ":
            rules.append("POL-002"); reasons.append("READ_ALLOWED")
            return out(Decision.ALLOW)
        if p.tier not in (0, 1, 2, 3):
            return deny("POL-020", "TIER_UNKNOWN")
        if p.action == "PROPOSE":
            if p.tier == 0:
                return deny("POL-002", "L0_READ_ONLY")
            rules.append("POL-003"); reasons.append("ADVISORY_ALLOWED")
            return out(Decision.ALLOW)

        # --- money-moving: fail closed on unknown state ---------------------------------
        required = {"amount": p.amount, "target": p.target, "currency_pair": p.currency_pair,
                    "quote_fresh": p.quote_fresh, "beneficiary_status": p.beneficiary_status,
                    "liquidity_ok": p.liquidity_ok, "kill_switch": p.kill_switch,
                    "audit_available": p.audit_available, "fx_share_pct": p.fx_share_pct}
        missing = [k for k, v in required.items() if v is None]
        if missing:
            return deny("POL-020", "CRITICAL_STATE_UNKNOWN:" + ",".join(missing))
        if (len(p.currency_pair) != 2 or p.currency_pair[0] == p.currency_pair[1]
                or p.amount.ccy != p.currency_pair[0] or p.target.ccy != p.currency_pair[1]):
            return deny("POL-022", "CURRENCY_PAIR_AMBIGUOUS_OR_INCONSISTENT")
        if not p.audit_available:
            return deny("POL-019", "AUDIT_UNAVAILABLE_FAIL_CLOSED")
        if p.kill_switch:
            return deny("POL-014", "KILL_SWITCH_ACTIVE")
        if p.tier == 0:
            return deny("POL-002", "L0_CANNOT_EXECUTE")
        if p.tier == 1:
            return deny("POL-003", "L1_CANNOT_EXECUTE")
        if p.duplicate_of:
            rules.append("POL-017"); reasons.append(f"DUPLICATE_OF:{p.duplicate_of}")
            return out(Decision.RETURN_EXISTING)
        if p.invoice_flags:
            return deny("POL-018", "INVOICE_INTEGRITY:" + ",".join(p.invoice_flags))
        if p.beneficiary_status == "HOLD":
            rules.append("POL-010")
            return deny("POL-011", "NEW_BENEFICIARY_48H_HOLD")
        if p.beneficiary_status != "VERIFIED":
            return deny("POL-010", f"BENEFICIARY_{p.beneficiary_status}")
        if not p.quote_fresh:
            return deny("POL-015", "QUOTE_STALE_OR_MISSING")
        if not p.liquidity_ok:
            return deny("POL-016", "LIQUIDITY_RESERVE_BREACH")
        if p.amount.ccy != self.caps.ccy:
            return deny("POL-022", "CAP_CURRENCY_MISMATCH")
        if p.amount.minor > self.caps.per_payment_minor or "PER_PAYMENT_CAP_EXCEEDED" in p.cap_reasons:
            return deny("POL-006", "PER_PAYMENT_HARD_CAP_EXCEEDED")
        if "DAILY_CAP_EXCEEDED" in p.cap_reasons:
            return deny("POL-007", "DAILY_CAP_EXCEEDED")
        if "ROLLING_30D_CAP_EXCEEDED" in p.cap_reasons:
            return deny("POL-008", "ROLLING_30D_CAP_EXCEEDED")
        if p.approval.startswith("INVALID"):
            why = p.approval.split(":", 1)[-1]
            rule = "POL-021" if why == "FINGERPRINT_MISMATCH" else "POL-012"
            return deny(rule, f"APPROVAL_{why}")
        if p.recheck:
            rules.append("POL-013"); reasons.append("POLICY_RECHECKED_AT_EXECUTION")

        ask_reasons = []
        if p.tier == 2 and p.approval != "VALID":
            rules.append("POL-004"); ask_reasons.append("L2_EXACT_APPROVAL_REQUIRED")
        if p.tier == 3 and p.amount.minor > self.caps.autonomous_minor and p.approval != "VALID":
            rules.append("POL-005"); ask_reasons.append("ABOVE_AUTONOMOUS_CAP")
        if p.fx_share_pct > self.caps.max_fx_share_pct and p.approval != "VALID":
            rules.append("POL-009"); ask_reasons.append("FX_SHARE_ABOVE_LIMIT")
        if ask_reasons:
            reasons.extend(ask_reasons)
            return out(Decision.ASK, required_approval={"type": "EXACT_ACTION", "ttl_minutes": int(self.approval_ttl.total_seconds() // 60),
                                                        "binds": ["intent_id", "amount", "currency_pair", "beneficiary_version",
                                                                  "route", "quote", "execution_date", "policy_version"]},
                       expires_at=(now + self.approval_ttl).isoformat())
        rules.append("POL-005" if p.tier == 3 else "POL-004")
        reasons.append("WITHIN_MANDATE" if p.approval != "VALID" else "EXACT_APPROVAL_VALID")
        return out(Decision.ALLOW)
