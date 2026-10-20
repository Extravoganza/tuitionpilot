"""Permission tiers and the policy engine.

Principle: the model proposes, the policy disposes. Every action the agent
wants to take - whether proposed by the rule-based planner or by an LLM - is
evaluated here, deterministically, before anything touches money. The agent
has no tool that can change this policy; only the user can, via a separate
authenticated channel.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, IntEnum

from .models import D


class Tier(IntEnum):
    READ_ONLY = 0    # L0: observe balances, rates, deadlines
    ADVISE = 1       # L1: + quotes, plans, recommendations; cannot move money
    CONFIRM = 2      # L2: may execute, but every money movement needs user confirmation
    AUTONOMOUS = 3   # L3: executes alone inside limits; above limits falls back to L2


class ActionType(str, Enum):
    READ = "read"
    QUOTE = "quote"
    PLAN = "plan"
    CONVERT_FX = "convert_fx"
    PAY = "pay"
    ADD_PAYEE = "add_payee"
    CHANGE_POLICY = "change_policy"


MONEY_MOVING = {ActionType.CONVERT_FX, ActionType.PAY}
EXECUTING = MONEY_MOVING | {ActionType.ADD_PAYEE}


class Verdict(str, Enum):
    ALLOW = "allow"
    CONFIRM = "require_confirmation"
    DENY = "deny"


@dataclass
class PolicyConfig:
    tier: Tier = Tier.CONFIRM
    autonomous_max_usd: Decimal = D(2000)     # L3 acts alone only up to this per action
    per_tx_limit_usd: Decimal = D(15000)      # hard cap, even with confirmation
    daily_limit_usd: Decimal = D(20000)
    monthly_limit_usd: Decimal = D(30000)
    allowed_rails: tuple = ("remit", "edu", "local", "swift")
    new_payee_cooloff_hours: int = 48
    blocked_countries: tuple = ()             # compliance list, set by operator
    max_fx_single_conversion_pct: Decimal = D(60)   # of total planned need
    confirmation_ttl_minutes: int = 15
    kill_switch: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tier"] = f"L{int(self.tier)} {self.tier.name}"
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


@dataclass
class Action:
    type: ActionType
    usd_value: Decimal = D(0)
    payee_id: str | None = None
    rail_id: str | None = None
    country: str | None = None
    payee_verified: bool = True
    payee_added_at: datetime | None = None
    guard_blocking: bool = False
    guard_notes: list = field(default_factory=list)
    share_of_need_pct: Decimal | None = None
    description: str = ""
    params: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        blob = json.dumps({"t": self.type.value, "v": str(self.usd_value), "p": self.payee_id,
                           "r": self.rail_id, "params": self.params}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Decision:
    verdict: Verdict
    reasons: list
    rules: list

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "reasons": self.reasons, "rules": self.rules}


class PolicyEngine:
    def __init__(self, config: PolicyConfig, now, token_source=None):
        self.config = config
        self._now = now
        # production: cryptographically random codes; sandbox may inject a seeded source
        self._token = token_source or (lambda: secrets.token_hex(4).upper())
        self._spent: list[tuple[datetime, Decimal]] = []
        self._pending: dict[str, dict] = {}

    # ---- spend accounting ----------------------------------------------
    def record_spend(self, usd: Decimal) -> None:
        self._spent.append((self._now(), D(usd)))

    def spent_since(self, delta: timedelta) -> Decimal:
        cutoff = self._now() - delta
        return sum((v for t, v in self._spent if t >= cutoff), D(0))

    # ---- evaluation -----------------------------------------------------
    def evaluate(self, a: Action) -> Decision:
        c = self.config
        reasons, rules = [], []

        def deny(rule, why):
            rules.append(rule); reasons.append(why)
            return Decision(Verdict.DENY, reasons, rules)

        if a.type == ActionType.CHANGE_POLICY:
            return deny("P0-no-self-escalation", "the agent can never change its own permissions or limits")
        if c.kill_switch and a.type in EXECUTING:
            return deny("P1-kill-switch", "kill switch is engaged by the user")
        if a.type in (ActionType.READ,):
            return Decision(Verdict.ALLOW, ["read-only action"], ["P2-read"])
        if c.tier == Tier.READ_ONLY:
            return deny("P3-tier", "tier L0 READ_ONLY allows observation only")
        if a.type in (ActionType.QUOTE, ActionType.PLAN):
            return Decision(Verdict.ALLOW, ["advisory action"], ["P2-advise"])
        if c.tier == Tier.ADVISE:
            return deny("P3-tier", "tier L1 ADVISE can recommend but not execute")

        # ---- executing actions from here -------------------------------
        if a.country and a.country.upper() in {x.upper() for x in c.blocked_countries}:
            return deny("C1-compliance-country", f"destination {a.country} is on the operator's restricted list")
        if a.rail_id and a.rail_id not in c.allowed_rails:
            return deny("C2-rail", f"rail '{a.rail_id}' is not enabled by the user")
        if a.guard_blocking:
            return deny("S1-guard", "security guard raised a blocking finding: " + "; ".join(a.guard_notes or ["see report"]))
        if a.type in MONEY_MOVING:
            if a.usd_value > c.per_tx_limit_usd:
                return deny("L1-per-tx", f"{a.usd_value:.2f} USD exceeds per-transaction cap {c.per_tx_limit_usd}")
            day = self.spent_since(timedelta(days=1))
            if day + a.usd_value > c.daily_limit_usd:
                return deny("L2-daily", f"would exceed daily limit ({day:.2f}+{a.usd_value:.2f} > {c.daily_limit_usd})")
            month = self.spent_since(timedelta(days=30))
            if month + a.usd_value > c.monthly_limit_usd:
                return deny("L3-monthly", f"would exceed 30-day limit ({c.monthly_limit_usd} USD)")
            if (a.type == ActionType.CONVERT_FX and a.share_of_need_pct is not None
                    and a.share_of_need_pct > c.max_fx_single_conversion_pct):
                return deny("L4-fx-concentration",
                            f"single conversion of {a.share_of_need_pct}% of need exceeds {c.max_fx_single_conversion_pct}%")

        needs_confirm = []
        if a.type == ActionType.ADD_PAYEE:
            needs_confirm.append(("S2-new-payee", "adding a payee always needs the user"))
        if a.type == ActionType.PAY:
            if not a.payee_verified:
                needs_confirm.append(("S3-unverified-payee", "payee not verified out-of-band"))
            if a.payee_added_at and self._now() - a.payee_added_at < timedelta(hours=c.new_payee_cooloff_hours):
                needs_confirm.append(("S4-cooloff", f"payee added less than {c.new_payee_cooloff_hours}h ago"))
        if c.tier == Tier.CONFIRM and a.type in MONEY_MOVING:
            needs_confirm.append(("P4-tier-confirm", "tier L2 requires confirmation for money movement"))
        if c.tier == Tier.AUTONOMOUS and a.usd_value > c.autonomous_max_usd:
            needs_confirm.append(("P5-autonomy-cap", f"{a.usd_value:.2f} USD is above autonomous cap {c.autonomous_max_usd}"))

        if needs_confirm:
            for r, why in needs_confirm:
                rules.append(r); reasons.append(why)
            return Decision(Verdict.CONFIRM, reasons, rules)
        return Decision(Verdict.ALLOW, ["within L3 autonomous mandate and all limits"], ["P6-autonomous"])

    # ---- step-up confirmation ------------------------------------------
    def issue_confirmation(self, a: Action) -> str:
        token = self._token()
        self._pending[token] = {"fp": a.fingerprint(), "action": a,
                                "expires": self._now() + timedelta(minutes=self.config.confirmation_ttl_minutes)}
        return token

    def redeem(self, token: str) -> Action | None:
        """One-time, expiring token bound to the exact action fingerprint."""
        p = self._pending.pop(token.upper(), None)
        if not p or self._now() > p["expires"]:
            return None
        if p["action"].fingerprint() != p["fp"]:
            return None
        return p["action"]

    def purge_expired(self) -> list[str]:
        gone = [t for t, p in self._pending.items() if self._now() > p["expires"]]
        for t in gone:
            self._pending.pop(t, None)
        return gone

    def pending(self) -> list[dict]:
        return [{"token": t, "action": p["action"].description, "expires": p["expires"].isoformat()}
                for t, p in self._pending.items()]
