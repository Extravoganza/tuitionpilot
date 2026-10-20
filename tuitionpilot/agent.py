"""TuitionPilot - an AI agent that participates in a student's cross-border finances.

It does not just advise: it reads invoices, compares payment routes, buys the
study currency on a deadline-safe schedule and pays tuition/rent itself -
always through the policy engine, with every step written to a tamper-evident
audit log.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from .audit import AuditLog
from .bank import InsufficientFunds, SandboxBank, WALLET_FX_SPREAD
from .budget import budget_report
from .clock import SimClock
from .fxplan import FXPlan, due_tranches, make_plan
from .guards import GuardReport, check_amount, check_payee, scan_text
from .invoice import parse_invoice
from .market import FXMarket
from .models import D, Money, Obligation, ObligationKind, Payee, StudentProfile, q2
from .policy import Action, ActionType, PolicyEngine, Verdict
from .rails import RAILS, quote, rank_routes


@dataclass
class Result:
    status: str                 # ok | executed | pending_confirmation | denied | quarantined | error
    message: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "data": self.data}


class TuitionPilot:
    SETTLE_BUFFER_DAYS = 3

    def __init__(self, profile: StudentProfile, clock: SimClock, market: FXMarket,
                 bank: SandboxBank, policy: PolicyEngine, audit: AuditLog, planner=None):
        self.profile = profile
        self.clock = clock
        self.market = market
        self.bank = bank
        self.policy = policy
        self.audit = audit
        self.planner = planner
        self.obligations: dict[str, Obligation] = {}
        self.guard_reports: dict[str, GuardReport] = {}
        self.plans: dict[str, FXPlan] = {}
        self.strategy: dict[str, str] = {}
        self.monthly_spend: dict[str, Money] = {}
        self.expected_amounts: dict[str, Decimal] = {}
        self.notifications: list[dict] = []
        self._deferred: dict[str, Callable[[], Result]] = {}
        self._seq = 0

    # =================================================================== utils
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:03d}"

    def _usd(self, m: Money) -> Decimal:
        return q2(m.amount * self.market.mid(m.currency, "USD", self.clock.now()))

    def notify(self, level: str, text: str, **data) -> None:
        n = {"ts": self.clock.now().isoformat(), "level": level, "text": text, **data}
        self.notifications.append(n)
        self.audit.record("agent", "notify_user", level=level, text=text, **data)

    def _gate(self, action: Action, run: Callable[[], Result]) -> Result:
        """Single choke point: every executing action goes through the policy."""
        self.audit.record("agent", "action_proposed", type=action.type.value,
                          description=action.description, usd_value=str(action.usd_value),
                          fingerprint=action.fingerprint())
        dec = self.policy.evaluate(action)
        self.audit.record("policy", "decision", fingerprint=action.fingerprint(), **dec.to_dict())
        if dec.verdict == Verdict.DENY:
            self.notify("warning", f"Blocked: {action.description}", reasons=dec.reasons)
            return Result("denied", "; ".join(dec.reasons), {"decision": dec.to_dict()})
        if dec.verdict == Verdict.CONFIRM:
            token = self.policy.issue_confirmation(action)
            self._deferred[token] = lambda: self._run_after_recheck(action, run)
            self.notify("confirm", f"Needs your confirmation: {action.description}",
                        token=token, reasons=dec.reasons)
            return Result("pending_confirmation", f"confirm with code {token}",
                          {"token": token, "decision": dec.to_dict()})
        return self._commit(action, run)

    def _commit(self, action: Action, run: Callable[[], Result]) -> Result:
        try:
            res = run()
        except InsufficientFunds as e:
            self.audit.record("agent", "execution_failed", error=str(e))
            return Result("error", f"insufficient funds: {e}")
        if action.type in (ActionType.PAY, ActionType.CONVERT_FX) and res.status == "executed":
            self.policy.record_spend(action.usd_value)
        return res

    def _run_after_recheck(self, action: Action, run: Callable[[], Result]) -> Result:
        # Re-evaluate at execution time: state may have changed (kill switch, limits).
        dec = self.policy.evaluate(action)
        self.audit.record("policy", "recheck_on_confirm", fingerprint=action.fingerprint(), **dec.to_dict())
        if dec.verdict == Verdict.DENY:
            return Result("denied", "; ".join(dec.reasons), {"decision": dec.to_dict()})
        return self._commit(action, run)

    # ================================================================ user API
    def confirm(self, token: str) -> Result:
        self.audit.record("user", "confirmation_submitted", token=token.upper())
        action = self.policy.redeem(token)
        fn = self._deferred.pop(token.upper(), None)
        if action is None or fn is None:
            self.audit.record("policy", "confirmation_rejected", token=token.upper(),
                              reason="unknown, used or expired code")
            return Result("denied", "confirmation code is invalid, already used or expired")
        return fn()

    def set_kill_switch(self, on: bool) -> Result:
        self.policy.config.kill_switch = on
        self.audit.record("user", "kill_switch", engaged=on)
        if on:
            for t in list(self._deferred):
                self.policy.redeem(t)
                self._deferred.pop(t, None)
            self.notify("info", "Kill switch engaged: all autonomous activity stopped, pending confirmations voided")
        return Result("ok", f"kill switch {'ON' if on else 'OFF'}")

    def verify_payee(self, payee_id: str, channel: str) -> Result:
        """User confirms account details via an official, independent channel."""
        p = self.bank.payees[payee_id]
        p.verified = True
        self.audit.record("user", "payee_verified_out_of_band", payee_id=payee_id, channel=channel)
        return Result("ok", f"{p.name} marked verified via {channel}")

    def set_monthly_spend(self, category: str, amount: Money) -> None:
        self.monthly_spend[category] = amount
        self.audit.record("user", "budget_set", category=category, amount=amount.to_dict())

    # =============================================================== read tools
    def get_balances(self) -> Result:
        self.policy.evaluate(Action(ActionType.READ))
        return Result("ok", "balances", {"balances": self.bank.snapshot()})

    def get_budget(self) -> Result:
        live = [o for o in self.obligations.values()
                if not (self.guard_reports.get(o.obligation_id) and self.guard_reports[o.obligation_id].blocking)]
        rep = budget_report(self.bank.balances, self.monthly_spend, live,
                            self.profile.study_currency, self.market, self.clock.now())
        self.audit.record("agent", "budget_report", alerts=rep["alerts"], runway=rep["runway_months"])
        return Result("ok", "budget", rep)

    def get_rate(self, base: str, quote_ccy: str) -> Result:
        r = self.market.mid(base, quote_ccy, self.clock.now())
        hist = self.market.history(base, quote_ccy, self.clock.now(), 7)
        return Result("ok", f"1 {base} = {r} {quote_ccy} (sandbox, simulated)",
                      {"mid": str(r), "history_7d": [str(h) for h in hist]})

    # ============================================================ invoice intake
    def ingest_invoice(self, text: str, source: str = "email",
                       kind: ObligationKind = ObligationKind.TUITION) -> Result:
        self.audit.record("agent", "invoice_received", source=source, chars=len(text),
                          sha256=hashlib.sha256(text.encode()).hexdigest())
        inv = parse_invoice(text)
        rep = scan_text(text)
        self.audit.record("agent", "invoice_parsed", fields=inv.to_dict())
        if inv.missing:
            self.audit.record("guard", "invoice_incomplete", missing=inv.missing)
            return Result("error", f"invoice is missing: {', '.join(inv.missing)}", {"parsed": inv.to_dict()})

        known = next((p for p in self.bank.payees.values()
                      if p.name.lower() == (inv.payee or "").lower()), None)
        check_payee(rep, inv.account, known.account if known and known.verified else None)
        check_amount(rep, inv.amount, self.expected_amounts.get(kind.value))
        self.audit.record("guard", "scan_result", **rep.to_dict())

        ob_id = self._id("OB")
        if known and not rep.payee_mismatch:
            payee = known
        else:
            payee = Payee(self._id("PY"), inv.payee, inv.account, inv.currency,
                          (inv.country or self.profile.study_country), verified=False, category=kind)
        ob = Obligation(ob_id, kind, payee.payee_id, Money(inv.amount, inv.currency), inv.due,
                        inv.reference or "", source="invoice")
        self.obligations[ob_id] = ob
        self.guard_reports[ob_id] = rep

        if rep.blocking:
            self.audit.record("guard", "obligation_quarantined", obligation_id=ob_id, notes=rep.notes,
                              injection=rep.injection)
            if rep.payee_mismatch:
                self.bank.payees.setdefault(payee.payee_id, payee)
                self.bank.audit.record("sandbox", "payee_staged_unverified", payee=payee.to_dict())
            self.notify("alert", "Suspicious invoice quarantined - nothing will be paid. "
                        "Verify the bank details by calling the university using the number on its "
                        "official website (not the one in the email).",
                        obligation_id=ob_id, risk_score=rep.risk_score)
            return Result("quarantined", "invoice quarantined by security guards",
                          {"obligation": ob.to_dict(), "guard": rep.to_dict(), "parsed": inv.to_dict()})

        if payee.payee_id not in self.bank.payees:
            act = Action(ActionType.ADD_PAYEE, payee_id=payee.payee_id, country=payee.country,
                         description=f"add new payee {payee.name} ({payee.account})",
                         params={"account": payee.account})
            res = self._gate(act, lambda: (self.bank.add_payee(payee), Result("executed", "payee added"))[1])
            return Result(res.status if res.status != "executed" else "ok",
                          f"obligation {ob_id} created; new payee: {res.message}",
                          {"obligation": ob.to_dict(), "guard": rep.to_dict(), "payee_gate": res.to_dict()})
        self.notify("info", f"New {kind.value} obligation {ob.amount} due {ob.due.date()}", obligation_id=ob_id)
        return Result("ok", f"obligation {ob_id} created", {"obligation": ob.to_dict(), "guard": rep.to_dict()})

    def add_obligation(self, kind: ObligationKind, payee_id: str, amount: Money, due: datetime,
                       reference: str = "", source: str = "manual") -> Obligation:
        ob = Obligation(self._id("OB"), kind, payee_id, amount, due, reference, source=source)
        self.obligations[ob.obligation_id] = ob
        self.guard_reports[ob.obligation_id] = GuardReport()
        self.audit.record("user", "obligation_added", obligation=ob.to_dict())
        return ob

    # =========================================================== planning tools
    def _advisory_allowed(self, kind: ActionType, what: str) -> Result | None:
        dec = self.policy.evaluate(Action(kind, description=what))
        if dec.verdict == Verdict.DENY:
            self.audit.record("policy", "decision", **dec.to_dict(), description=what)
            return Result("denied", "; ".join(dec.reasons), {"decision": dec.to_dict()})
        return None

    def quote_routes(self, obligation_id: str) -> Result:
        blocked = self._advisory_allowed(ActionType.QUOTE, f"quote routes for {obligation_id}")
        if blocked:
            return blocked
        ob = self.obligations[obligation_id]
        allowed = list(self.policy.config.allowed_rails)
        qs = rank_routes(self.market, self.profile.home_currency, ob.amount, self.clock.now(),
                         deadline=ob.due - timedelta(days=1), allowed=allowed)
        self.audit.record("agent", "routes_quoted", obligation_id=obligation_id,
                          quotes=[q.to_dict() for q in qs])
        return Result("ok", "routes", {"quotes": [q.to_dict() for q in qs]})

    def plan_obligation(self, obligation_id: str) -> Result:
        blocked = self._advisory_allowed(ActionType.PLAN, f"plan {obligation_id}")
        if blocked:
            return blocked
        ob = self.obligations[obligation_id]
        if self.guard_reports.get(obligation_id) and self.guard_reports[obligation_id].blocking:
            return Result("denied", "obligation is quarantined")
        now = self.clock.now()
        src = self.profile.home_currency
        routes = rank_routes(self.market, src, ob.amount, now, deadline=ob.due - timedelta(days=1),
                             allowed=list(self.policy.config.allowed_rails))
        best_direct = next((q for q in routes if q.eligible), None)
        mid = self.market.mid(src, ob.amount.currency, now)
        staged_cost_src = ob.amount.amount / (mid * (1 - WALLET_FX_SPREAD))
        rationale = []
        if src == ob.amount.currency:
            strategy = "direct"
            rationale.append("same currency - no conversion needed")
        elif best_direct is None or staged_cost_src < best_direct.send.amount:
            strategy = "staged_fx"
            rationale.append(f"staged wallet conversion ~{q2(staged_cost_src)} {src} vs best direct route "
                             f"{best_direct.send if best_direct else 'n/a'}")
        else:
            strategy = "direct"
            rationale.append(f"best direct route {best_direct.rail_name} is cheapest now")
        self.strategy[obligation_id] = strategy
        data = {"strategy": strategy, "rationale": rationale,
                "best_direct": best_direct.to_dict() if best_direct else None,
                "staged_estimate_src": str(q2(staged_cost_src))}
        if strategy == "staged_fx":
            already = self.bank.balance(ob.amount.currency).amount - self._reserved(ob.amount.currency, exclude=obligation_id)
            need = max(D(0), ob.amount.amount - max(D(0), already))
            plan = make_plan(self._id("FXP"), src, Money(need, ob.amount.currency), now, ob.due,
                             self.SETTLE_BUFFER_DAYS, self.profile.risk_preference)
            self.plans[obligation_id] = plan
            data["fx_plan"] = plan.to_dict()
        self.audit.record("agent", "plan_created", obligation_id=obligation_id, **{k: v for k, v in data.items()})
        return Result("ok", f"plan: {strategy}", data)

    def _reserved(self, ccy: str, exclude: str | None = None) -> Decimal:
        """Study-currency already earmarked for other unpaid obligations."""
        tot = D(0)
        for oid, ob in self.obligations.items():
            if oid != exclude and not ob.paid and ob.amount.currency == ccy and oid in self.plans:
                plan = self.plans[oid]
                tot += sum((t.amount_dst for t in plan.tranches if t.done), D(0))
        return tot

    # ========================================================= executing tools
    def convert_tranche(self, obligation_id: str, tranche_index: int, trigger: str) -> Result:
        plan = self.plans[obligation_id]
        t = plan.tranches[tranche_index]
        if t.done:
            return Result("ok", "tranche already executed")
        mid = self.market.mid(plan.src, plan.dst, self.clock.now())
        src_amt = q2(t.amount_dst / (mid * (1 - WALLET_FX_SPREAD)) + D("0.01"))
        share = q2(t.amount_dst / plan.need.amount * 100) if plan.need.amount else D(0)
        act = Action(ActionType.CONVERT_FX, usd_value=self._usd(Money(src_amt, plan.src)),
                     share_of_need_pct=share, rail_id=None,
                     description=f"convert {src_amt} {plan.src} -> {t.amount_dst} {plan.dst} "
                                 f"(tranche {tranche_index+1}/{len(plan.tranches)}, {trigger})",
                     params={"ob": obligation_id, "t": tranche_index})

        def run() -> Result:
            key = f"{plan.plan_id}:T{tranche_index}"
            rec = self.bank.convert(plan.src, plan.dst, src_amt, key)
            t.done, t.executed_at, t.rate, t.trigger = True, self.clock.now(), D(rec["rate"]), trigger
            return Result("executed", f"converted tranche {tranche_index+1}", {"conversion": rec})
        return self._gate(act, run)

    def pay(self, obligation_id: str) -> Result:
        ob = self.obligations[obligation_id]
        if ob.paid:
            return Result("ok", "already paid")
        rep = self.guard_reports.get(obligation_id) or GuardReport()
        payee = self.bank.payees.get(ob.payee_id)
        if payee is None:
            return Result("error", "payee not registered yet")
        now = self.clock.now()
        dst = ob.amount.currency
        available = self.bank.balance(dst).amount - self._reserved(dst, exclude=obligation_id)
        if available >= ob.amount.amount:
            qt = quote(RAILS["local"], self.market, dst, ob.amount, now, deadline=ob.due)
        else:
            routes = rank_routes(self.market, self.profile.home_currency, ob.amount, now,
                                 deadline=ob.due, allowed=list(self.policy.config.allowed_rails))
            qt = next((q for q in routes if q.eligible), None)
            if qt is None:
                self.notify("alert", f"No eligible route can deliver {ob.amount} before {ob.due.date()}")
                return Result("error", "no eligible route")
        act = Action(ActionType.PAY, usd_value=self._usd(ob.amount), payee_id=payee.payee_id,
                     rail_id=qt.rail_id, country=payee.country, payee_verified=payee.verified,
                     payee_added_at=payee.added_at, guard_blocking=rep.blocking, guard_notes=rep.notes,
                     description=f"pay {ob.kind.value} {ob.amount} to {payee.name} via {qt.rail_name} "
                                 f"(ref {ob.reference or '-'})",
                     params={"ob": obligation_id, "rail": qt.rail_id})

        def run() -> Result:
            tx = self.bank.send(payee.payee_id, qt, ob.reference, f"PAY:{obligation_id}")
            ob.paid = True
            self.notify("success", f"Paid {ob.amount} to {payee.name}; arrives by {tx.arrives_by.date()}",
                        tx_id=tx.tx_id)
            return Result("executed", f"payment submitted {tx.tx_id}", {"transfer": tx.to_dict(), "quote": qt.to_dict()})
        return self._gate(act, run)

    def request_policy_change(self, change: dict, reason: str) -> Result:
        """Exists so that an LLM asking to raise its own limits is visibly refused."""
        act = Action(ActionType.CHANGE_POLICY, description=f"agent asked to change policy: {change}",
                     params=change)
        return self._gate(act, lambda: Result("executed", "unreachable"))

    # ============================================================ autonomy loop
    def tick(self) -> list[Result]:
        """One heartbeat of the autonomous agent (called e.g. hourly/daily)."""
        out = []
        for tx in self.bank.settle_due():
            self.notify("info", f"Transfer {tx.tx_id} settled: {tx.credit} delivered")
        if self.policy.config.kill_switch:
            return out
        now = self.clock.now()
        for oid, ob in self.obligations.items():
            if ob.paid or (self.guard_reports.get(oid) and self.guard_reports[oid].blocking):
                continue
            plan = self.plans.get(oid)
            if plan:
                for t, why in due_tranches(plan, self.market, now):
                    out.append(self.convert_tranche(oid, t.index, why))
            available = self.bank.balance(ob.amount.currency).amount - self._reserved(ob.amount.currency, exclude=oid)
            funded = available >= ob.amount.amount
            pay_window = ob.due - timedelta(days=self.SETTLE_BUFFER_DAYS)
            # no FX plan -> pay directly (cross-border route) when the window opens
            ready = (plan is None) or (plan.complete and funded)
            if ready and now >= pay_window - timedelta(days=4) and not self._has_pending_for(oid):
                out.append(self.pay(oid))
        return out

    def _has_pending_for(self, oid: str) -> bool:
        for t in self.policy.purge_expired():
            self._deferred.pop(t, None)
            self.audit.record("policy", "confirmation_expired", token=t)
        return any(p["action"].params.get("ob") == oid and p["action"].type == ActionType.PAY
                   for p in self.policy._pending.values())

    # ============================================================ conversation
    def handle(self, message: str) -> dict:
        """Natural-language entry point. The planner turns text into tool calls;
        each tool call is validated against the allowlist before running."""
        self.audit.record("user", "message", text=message[:500])
        if self.planner is None:
            from .planner import RuleBasedPlanner
            self.planner = RuleBasedPlanner()
        calls = self.planner.plan(message, self.context_summary())
        results = []
        for call in calls:
            results.append({"tool": call["tool"], "args": call.get("args", {}),
                            "result": self.call_tool(call["tool"], call.get("args", {})).to_dict()})
        reply = self.planner.summarise(message, results) if hasattr(self.planner, "summarise") else None
        return {"calls": results, "reply": reply}

    TOOLS = {"get_balances", "get_budget", "get_rate", "quote_routes", "plan_obligation",
             "pay", "convert_tranche", "request_policy_change", "list_obligations"}

    def call_tool(self, name: str, args: dict) -> Result:
        if name not in self.TOOLS:
            self.audit.record("guard", "tool_rejected", tool=name, reason="not in allowlist")
            return Result("denied", f"tool '{name}' is not available to the agent")
        try:
            if name == "list_obligations":
                return Result("ok", "obligations", {"obligations": [o.to_dict() for o in self.obligations.values()]})
            if name == "get_rate":
                return self.get_rate(args.get("base", self.profile.home_currency),
                                     args.get("quote", self.profile.study_currency))
            if name == "convert_tranche":
                return self.convert_tranche(str(args["obligation_id"]), int(args["tranche_index"]),
                                            str(args.get("trigger", "requested")))
            if name in ("quote_routes", "plan_obligation", "pay"):
                oid = str(args.get("obligation_id") or self._default_obligation() or "")
                if oid not in self.obligations:
                    return Result("error", "no such obligation")
                return getattr(self, name)(oid)
            if name == "request_policy_change":
                return self.request_policy_change(dict(args.get("change", {})), str(args.get("reason", "")))
            return getattr(self, name)()
        except (KeyError, ValueError, TypeError) as e:
            self.audit.record("guard", "tool_args_invalid", tool=name, error=str(e))
            return Result("error", f"invalid arguments for {name}: {e}")

    def _default_obligation(self) -> str | None:
        unpaid = [o for o in self.obligations.values() if not o.paid
                  and not (self.guard_reports.get(o.obligation_id) and self.guard_reports[o.obligation_id].blocking)]
        return min(unpaid, key=lambda o: o.due).obligation_id if unpaid else None

    def context_summary(self) -> dict:
        return {"now": self.clock.now().isoformat(), "tier": self.policy.config.to_dict()["tier"],
                "balances": self.bank.snapshot(),
                "obligations": [o.to_dict() for o in self.obligations.values()],
                "home_currency": self.profile.home_currency, "study_currency": self.profile.study_currency}
