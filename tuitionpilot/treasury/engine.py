"""TreasuryEngine: wires the 2.0 modules together and runs every money-moving
action through one path:

    propose → (approval if ASK) → execute: re-validate everything → atomic cap
    reservation → audit write (fail closed) → sandbox settlement → audit write

Execution is SIMULATION ONLY. States never say "paid": the terminal success
state is SIMULATED_SUBMITTED.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

from ..guards import scan_text
from .beneficiary import BeneficiaryRegistry, PAYMENT_FIELDS
from .calendar import BusinessCalendar
from .currency import Amount
from .intents import ApprovalService, ExecutionAttempt, IntentSpec, IntentStore
from .ledger import AuditUnavailable, Ledger, POLICY_VERSION
from .limits import Caps, LimitBook
from .liquidity import CashFlow, LiquidityBook
from .optimizer import Obligation, OptimizerInputs, PlanDecision, optimize
from .policy import Decision, PolicyEngine, PolicyInput
from . import fixtures as F


class SimulatedTimeout(RuntimeError):
    """The sandbox provider did not answer in time. The caller must retry with the same key."""


@dataclass
class Clock:
    t: datetime

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t = self.t + timedelta(**kw)
        return self.t


class TreasuryEngine:
    def __init__(self, user_id: str = "student-001", tier: int = 3, seed: int = 7,
                 now: datetime = F.NOW, caps: Caps | None = None, calendar: BusinessCalendar = F.CALENDAR,
                 liquidity: dict | None = None):
        import random
        rng = random.Random(seed)
        self.seed = seed
        self.clock = Clock(now)
        self.user_id = user_id
        self.tier = tier
        self.kill_switch = False
        self.calendar = calendar
        self.caps = caps or Caps()
        self.ledger = Ledger(self.clock.now, correlation_id=f"RUN-{seed:04d}")
        self.registry = BeneficiaryRegistry()
        self.limits = LimitBook(self.caps)
        self.policy = PolicyEngine(self.caps)
        # sandbox: seeded tokens for reproducible replays; production uses secrets.token_urlsafe
        self.approvals = ApprovalService(token_source=lambda: "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(22)))
        self.intents = IntentStore()
        self.quotes = F.quotes_at(now)
        self.allowed_routes = set(F.ROUTES)
        self.outage: set = set()
        self.invoice_flags: dict = {}
        self.executed_obligations: set = set()
        self.paid_target: dict = {}
        self.paid_by: dict = {}
        self._ids = 0
        self._lock = threading.Lock()
        L = liquidity or F.LIQUIDITY
        self.liquidity = LiquidityBook("KZT", L["opening"], now.date(), L["reserve"],
                                       [CashFlow(d, a, k, lbl) for d, a, k, lbl in L["flows"]])
        # onboarding: verified beneficiary version (independent callback, months before)
        v1 = self.registry.add_version("BEN-SDU", F.BENEFICIARY, now - timedelta(days=138), "onboarding")
        self.registry.verify(v1.version_id, "trusted_callback", "phone number listed on the university's official website",
                             "student-001", now - timedelta(days=137))
        ob = F.TUITION
        self.obligations = {ob["obligation_id"]: Obligation(ob["obligation_id"], ob["target"], ob["due"],
                                                            ob["processing_buffer_bd"], "BEN-SDU", v1.version_id,
                                                            ob["reference"])}
        self._log("system", "run_started", "engine", {"seed": seed, "tier": tier, "environment": "SIMULATION",
                                                       "label": "SIMULATION — NO REAL MONEY MOVES"})

    # ------------------------------------------------------------------ utils
    def _next(self, prefix: str) -> str:
        with self._lock:
            self._ids += 1
            return f"{prefix}-{self._ids:04d}"

    def _log(self, actor, etype, entity, payload):
        return self.ledger.append(actor, etype, entity, payload)

    def beneficiary_status(self, version_id: str) -> str:
        status, _ = self.registry.payable_status(version_id, self.clock.now())
        return status

    # ------------------------------------------------------------- user-only
    def set_kill_switch(self, on: bool, actor: str, reason: str) -> None:
        if actor != "user":
            raise PermissionError("only the authenticated user can change the kill switch")
        self.kill_switch = on
        self._log("user", "kill_switch", "autonomy", {"engaged": on, "reason": reason})

    # ---------------------------------------------------------------- invoice
    def parse_invoice(self, text: str, beneficiary_id: str = "BEN-SDU") -> dict:
        """Invoice text is untrusted DATA. Fields are extracted, never obeyed."""
        def grab(label):
            m = re.search(rf"^\s*(?:{label})\s*[:\-]\s*(.+?)\s*$", text, re.I | re.M)
            return m.group(1).strip() if m else ""
        amt = re.search(r"^\s*amount(?: due)?\s*[:\-]\s*([A-Z]{3})\s*([0-9][0-9,\.]*)", text, re.I | re.M)
        fields = {"legal_name": grab("payee"), "account": grab("iban|account(?: number)?"), "bank_bic": grab("bic|swift"),
                  "country": grab("country"), "currency": amt.group(1).upper() if amt else ""}
        extracted = {**fields, "amount": amt.group(2).replace(",", "") if amt else "", "due": grab("due(?: date)?"),
                     "reference": grab("reference")}
        guard = scan_text(text)
        shield = self.registry.check_invoice(beneficiary_id, fields)
        flags = []
        if guard.injection:
            flags.append("PROMPT_INJECTION")
        if shield["status"] == "MISMATCH_BLOCKED":
            flags.append("BENEFICIARY_FIELDS_CHANGED")
            self.registry.add_version(beneficiary_id, fields, self.clock.now(), "invoice")
        if guard.urgency:
            flags.append("URGENCY_PRESSURE")
        inv_id = self._next("INV")
        self.invoice_flags[inv_id] = [f for f in flags if f in ("PROMPT_INJECTION", "BENEFICIARY_FIELDS_CHANGED")]
        out = {"invoice_id": inv_id, "extracted": {**extracted, "account": "•" * 4 + fields["account"][-4:] if fields["account"] else ""},
               "guard": {"injection": [i["label"] for i in guard.injection], "urgency": guard.urgency,
                         "change_language": guard.detail_change_language},
               "shield": shield, "flags": flags, "treated_as": "untrusted data"}
        self._log("agent", "invoice_parsed", inv_id, {"flags": flags, "shield_status": shield["status"],
                                                      "mismatched_fields": shield["mismatched_fields"]})
        return out

    # -------------------------------------------------------------- optimizer
    def optimizer_inputs(self, obligation_id: str, **over) -> OptimizerInputs:
        ob = self.obligations[obligation_id]
        inp = OptimizerInputs(now=self.clock.now(), obligation=ob, quotes=self.quotes, calendar=self.calendar,
                              liquidity=self.liquidity, caps=self.caps,
                              beneficiary_status=self.beneficiary_status(ob.beneficiary_version_id),
                              allowed_routes=self.allowed_routes, kill_switch=self.kill_switch,
                              already_executed=obligation_id in self.executed_obligations, outage=set(self.outage))
        return replace(inp, **over) if over else inp

    def plan(self, obligation_id: str, log: bool = True, **over) -> PlanDecision:
        d = optimize(self.optimizer_inputs(obligation_id, **over))
        if log:
            self._log("agent", "plan_decision", obligation_id,
                      {"status": d.status, "selected": d.selected.candidate_id if d.selected else None,
                       "selected_route": d.selected.route_id if d.selected else None,
                       "selected_strategy": d.selected.strategy if d.selected else None,
                       "total": str(d.selected.total) if d.selected else None,
                       "rejected": len(d.rejected()), "reasons_if_infeasible": d.reasons_if_infeasible})
        return d

    # ---------------------------------------------------------------- intents
    def intent_from_plan(self, obligation_id: str, decision: PlanDecision, tranche: int = 0) -> IntentSpec:
        if decision.selected is None:
            raise ValueError("no feasible plan; nothing to propose")
        ob = self.obligations[obligation_id]
        t = decision.selected.tranches[tranche]
        q = self.quotes[decision.selected.route_id]
        return IntentSpec(self._next("PI"), self.user_id, obligation_id, t.cost.all_in_known, t.target,
                          ob.beneficiary_version_id, q.route.route_id, q.quote_id, t.execution_date)

    def intent_from_invoice(self, invoice_id: str, obligation_id: str, decision: PlanDecision) -> IntentSpec:
        """An intent that pays the bank details found in an invoice (used to show it gets blocked)."""
        base = self.intent_from_plan(obligation_id, decision)
        latest = self.registry.history(self.obligations[obligation_id].beneficiary_id)[-1]
        return replace(base, beneficiary_version_id=latest.version_id, invoice_id=invoice_id)

    def _policy_input(self, spec: IntentSpec, session_user: str, approval: str = "MISSING", recheck: bool = False) -> PolicyInput:
        ob = self.obligations.get(spec.obligation_id)
        q = self.quotes.get(spec.route_id)
        quote_ok = (q is not None and q.quote_id == spec.quote_id and q.is_fresh(self.clock.now())
                    and spec.route_id in self.allowed_routes and spec.route_id not in self.outage)
        ben = self.beneficiary_status(spec.beneficiary_version_id)
        if ob and ob.beneficiary_version_id != spec.beneficiary_version_id:
            ben = "MISMATCH"
        flow = [CashFlow(spec.execution_date, spec.source_amount, "planned_outflow", spec.intent_id)]
        hr = self.limits.headroom(self.clock.now())
        cap_reasons = []
        if spec.source_amount.minor > self.caps.per_payment_minor:
            cap_reasons.append("PER_PAYMENT_CAP_EXCEEDED")
        if hr["daily_used"] + spec.source_amount.minor > self.caps.daily_minor:
            cap_reasons.append("DAILY_CAP_EXCEEDED")
        if hr["rolling_30d_used"] + spec.source_amount.minor > self.caps.rolling_30d_minor:
            cap_reasons.append("ROLLING_30D_CAP_EXCEEDED")
        # invoice integrity applies to intents built from that invoice; changed bank details are
        # caught separately by the beneficiary-version check (POL-010/011)
        inv_flags = [f for f in self.invoice_flags.get(spec.invoice_id, []) if f == "PROMPT_INJECTION"]
        share = (Decimal(spec.target_amount.minor) / Decimal(ob.target.minor) * 100) if ob else None
        paid = self.paid_target.get(spec.obligation_id, 0)
        dup = None
        if ob and paid + spec.target_amount.minor > ob.target.minor:
            dup = self.paid_by.get(spec.obligation_id, "existing execution")
        return PolicyInput(action="EXECUTE_PAYMENT", actor="engine", user_id=spec.user_id, session_user_id=session_user,
                           tier=self.tier, amount=spec.source_amount, target=spec.target_amount,
                           currency_pair=spec.currency_pair, quote_fresh=quote_ok, beneficiary_status=ben,
                           liquidity_ok=self.liquidity.ok(flow), kill_switch=self.kill_switch,
                           audit_available=self.ledger.available, invoice_flags=inv_flags,
                           fx_share_pct=share, cap_reasons=cap_reasons, duplicate_of=dup,
                           approval=approval, recheck=recheck)

    @staticmethod
    def _key(spec: IntentSpec) -> str:
        return f"{spec.obligation_id}:{spec.fingerprint()[:24]}"

    def evaluate(self, spec: IntentSpec, session_user: str, approval: str = "MISSING", recheck: bool = False):
        res = self.policy.evaluate(self._policy_input(spec, session_user, approval, recheck), self.clock.now())
        return res

    def request_approval(self, spec: IntentSpec, session_user: str) -> tuple[dict, str | None]:
        """Returns (public challenge info, raw token for the USER CHANNEL ONLY)."""
        res = self.evaluate(spec, session_user)
        try:
            self._log("policy", "policy_decision", spec.intent_id, {"intent": spec.summary(), **res.to_dict()})
        except AuditUnavailable:
            return {"decision": "DENY", "reason": "AUDIT_UNAVAILABLE_FAIL_CLOSED"}, None
        if res.decision != Decision.ASK:
            return {"decision": res.decision.value, "policy": res.to_dict()}, None
        ch, raw = self.approvals.issue(spec, self.clock.now())
        self._log("policy", "approval_challenge_issued", spec.intent_id, ch.public())
        return {"decision": "ASK", "challenge": ch.public(), "approval_screen": self.approval_screen(spec)}, raw

    def approval_screen(self, spec: IntentSpec) -> dict:
        v = self.registry.get(spec.beneficiary_version_id)
        q = self.quotes[spec.route_id]
        t0, t1 = self.calendar.arrival_range(spec.execution_date, q.route.arrival_min_bd, q.route.arrival_max_bd)
        return {"amount_debited": str(spec.source_amount), "amount_credited": str(spec.target_amount),
                "currency_pair": "/".join(spec.currency_pair), "recipient": v.masked() if v else None,
                "route": q.route.name, "quote_id": q.quote_id, "quote_expires_at": q.expires_at().isoformat(),
                "estimated_arrival": [t0.isoformat(), t1.isoformat()],
                "reference": self.obligations[spec.obligation_id].reference,
                "approval_expires_in_minutes": 15, "consent_prechecked": False,
                "label": "SIMULATION — NO REAL MONEY MOVES"}

    # -------------------------------------------------------------- execution
    def execute(self, spec: IntentSpec, session_user: str, approval_token: str | None = None,
                simulate_timeout: bool = False) -> dict:
        key = self._key(spec)
        existing = self.intents.get(key)
        if existing is not None:
            return self._resume(existing, spec)

        approval = "MISSING"
        ch = None
        if approval_token is not None:
            approval, ch = self.approvals.redeem(approval_token, spec, self.clock.now())
        res = self.evaluate(spec, session_user, approval, recheck=True)
        try:
            self._log("policy", "policy_decision", spec.intent_id,
                      {"intent": spec.summary(), "approval": approval, "approval_id": ch.approval_id if ch else None,
                       **res.to_dict()})
        except AuditUnavailable:
            return {"state": "NOT_EXECUTED", "decision": "DENY", "matched_rule_ids": ["POL-019"],
                    "reason_codes": ["AUDIT_UNAVAILABLE_FAIL_CLOSED"]}
        if res.decision != Decision.ALLOW:
            return {"state": "NOT_EXECUTED", **res.to_dict()}

        rsv, why = self.limits.try_reserve(spec.intent_id, spec.source_amount, self.clock.now())
        if rsv is None:
            rule = {"DAILY_CAP_EXCEEDED": "POL-007", "ROLLING_30D_CAP_EXCEEDED": "POL-008"}.get(why[0], "POL-006")
            self._log("policy", "cap_reservation_denied", spec.intent_id, {"reasons": why, "rule": rule})
            return {"state": "NOT_EXECUTED", "decision": "DENY", "matched_rule_ids": [rule], "reason_codes": why}

        attempt, created = self.intents.create(key, spec.intent_id)
        attempt.reservation_id = rsv.reservation_id
        try:
            self._log("executor", "execution_started", attempt.execution_id,
                      {"intent": spec.summary(), "reservation": rsv.reservation_id, "environment": "SIMULATION"})
        except AuditUnavailable:
            self.limits.release(rsv.reservation_id)
            attempt.state = "FAILED"
            return {"state": "NOT_EXECUTED", "decision": "DENY", "matched_rule_ids": ["POL-019"],
                    "reason_codes": ["AUDIT_UNAVAILABLE_FAIL_CLOSED"]}
        if simulate_timeout:
            raise SimulatedTimeout(f"sandbox provider timeout for {attempt.execution_id}; retry with the same intent")
        return self._settle(attempt, spec)

    def _settle(self, attempt: ExecutionAttempt, spec: IntentSpec) -> dict:
        self.liquidity.flows.append(CashFlow(spec.execution_date, spec.source_amount, "committed_outflow",
                                             f"{attempt.execution_id} {spec.route_id} (simulated)"))
        attempt.state = "SIMULATED_SUBMITTED"
        self.limits.commit(attempt.reservation_id)
        ob = self.obligations[spec.obligation_id]
        self.paid_target[spec.obligation_id] = self.paid_target.get(spec.obligation_id, 0) + spec.target_amount.minor
        self.paid_by[spec.obligation_id] = attempt.execution_id
        if self.paid_target[spec.obligation_id] >= ob.target.minor:
            self.executed_obligations.add(spec.obligation_id)
        self._log("executor", "execution_simulated", attempt.execution_id,
                  {"intent_id": spec.intent_id, "state": attempt.state, "debited": str(spec.source_amount),
                   "credited": str(spec.target_amount), "label": "SIMULATION — NO REAL MONEY MOVES"})
        return {"state": attempt.state, "decision": "ALLOW", **attempt.to_dict()}

    def _resume(self, attempt: ExecutionAttempt, spec: IntentSpec) -> dict:
        attempt.attempts += 1
        self._log("executor", "idempotent_retry", attempt.execution_id,
                  {"intent_id": spec.intent_id, "state_before": attempt.state, "attempts": attempt.attempts,
                   "rule": "POL-017"})
        if attempt.state == "EXECUTING":
            return {"retried": True, **self._settle(attempt, spec)}
        return {"retried": True, "decision": "RETURN_EXISTING", "matched_rule_ids": ["POL-017"], **attempt.to_dict()}

    def executions(self) -> list[ExecutionAttempt]:
        return list(self.intents.by_key.values())
