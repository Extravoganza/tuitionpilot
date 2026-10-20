"""The 9 typed, allow-listed tools the planner (LLM or deterministic) may call.

* At most 6 calls per turn.
* Arguments are validated against a schema; unknown fields are rejected.
* Every tool checks that the caller's user matches the data owner.
* No tool can change policy, caps, tier, payee verification or the ledger,
  and none returns an approval token: tokens go only to the user channel.
* Invoice text passed to ``parse_invoice`` is data, never instructions.
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import TreasuryEngine

MAX_CALLS_PER_TURN = 6

SCHEMAS = {
    "read_obligations": {},
    "read_balances": {},
    "parse_invoice": {"text": str},
    "quote_routes": {"obligation_id": str},
    "simulate_fx_plan": {"obligation_id": str, "rate_shock_pct": (int, float, str), "extra_delay_bd": int},
    "propose_payment_plan": {"obligation_id": str},
    "request_approval": {"intent_id": str},
    "execute_sandbox_payment": {"intent_id": str},
    "read_audit_events": {"limit": int},
}
OPTIONAL = {"simulate_fx_plan": {"rate_shock_pct", "extra_delay_bd"}, "read_audit_events": {"limit"}}


class ToolError(ValueError):
    pass


@dataclass
class ToolRouter:
    engine: TreasuryEngine
    session_user: str
    user_tokens: dict            # approval tokens delivered to the user, held outside the model

    def __post_init__(self):
        self.intents = {}

    def _validate(self, name: str, args: dict) -> dict:
        if name not in SCHEMAS:
            raise ToolError(f"tool '{name}' is not allow-listed")
        if not isinstance(args, dict):
            raise ToolError("arguments must be an object")
        schema = SCHEMAS[name]
        extra = set(args) - set(schema)
        if extra:
            raise ToolError(f"unknown argument(s): {sorted(extra)}")
        for k, typ in schema.items():
            if k not in args:
                if k in OPTIONAL.get(name, set()):
                    continue
                raise ToolError(f"missing argument '{k}'")
            if not isinstance(args[k], typ) or isinstance(args[k], bool):
                raise ToolError(f"argument '{k}' has the wrong type")
        if name == "simulate_fx_plan" and "rate_shock_pct" in args:
            v = float(args["rate_shock_pct"])
            if not -30 <= v <= 30:
                raise ToolError("rate_shock_pct must be within ±30")
        if name == "read_audit_events" and not 1 <= args.get("limit", 20) <= 200:
            raise ToolError("limit must be 1..200")
        return args

    def run_turn(self, calls: list) -> list:
        results = []
        for i, call in enumerate(calls):
            if i >= MAX_CALLS_PER_TURN:
                results.append({"tool": call.get("tool"), "status": "rejected", "error": "max 6 tool calls per turn"})
                self.engine._log("guard", "tool_call_rejected", "planner", {"reason": "MAX_CALLS_PER_TURN", "tool": call.get("tool")})
                continue
            try:
                args = self._validate(call.get("tool"), call.get("args", {}))
                results.append({"tool": call["tool"], "status": "ok", "result": getattr(self, "_" + call["tool"])(**args)})
            except ToolError as e:
                self.engine._log("guard", "tool_call_rejected", "planner", {"tool": str(call.get("tool")), "reason": str(e)})
                results.append({"tool": call.get("tool"), "status": "rejected", "error": str(e)})
        return results

    # ---- tools -------------------------------------------------------------
    def _scope(self):
        if self.session_user != self.engine.user_id:
            raise ToolError("caller is not the data owner")

    def _read_obligations(self):
        self._scope()
        return [{"obligation_id": o.obligation_id, "target": str(o.target), "due": o.due.isoformat(),
                 "latest_safe_arrival": o.latest_safe(self.engine.calendar).isoformat(), "reference": o.reference}
                for o in self.engine.obligations.values()]

    def _read_balances(self):
        self._scope()
        L = self.engine.liquidity
        return {"currency": L.ccy, "opening": str(L.opening), "reserve": str(L.reserve), "as_of": L.as_of.isoformat(),
                "projection": L.projection(), "label": "SIMULATED snapshot, not a live balance"}

    def _parse_invoice(self, text):
        self._scope()
        return self.engine.parse_invoice(text)

    def _quote_routes(self, obligation_id):
        self._scope()
        d = self.engine.plan(obligation_id, log=False)
        return d.quotes

    def _simulate_fx_plan(self, obligation_id, rate_shock_pct=0, extra_delay_bd=0):
        self._scope()
        from decimal import Decimal
        d = self.engine.plan(obligation_id, log=False, rate_shock=Decimal(str(rate_shock_pct)) / 100,
                             extra_delay_bd=int(extra_delay_bd))
        return {"status": d.status, "selected": d.selected.to_dict() if d.selected else None,
                "reasons_if_infeasible": d.reasons_if_infeasible, "label": "scenario, not a forecast"}

    def _propose_payment_plan(self, obligation_id):
        self._scope()
        d = self.engine.plan(obligation_id)
        if d.selected is None:
            return {"status": "INFEASIBLE", "reasons": d.reasons_if_infeasible}
        spec = self.engine.intent_from_plan(obligation_id, d)
        self.intents[spec.intent_id] = spec
        return {"status": "PROPOSED", "intent": spec.summary(), "plan": d.selected.candidate_id}

    def _request_approval(self, intent_id):
        self._scope()
        spec = self.intents.get(intent_id)
        if spec is None:
            raise ToolError("unknown intent")
        info, raw = self.engine.request_approval(spec, self.session_user)
        if raw:
            self.user_tokens[intent_id] = raw          # to the user's device, never back to the model
        return info

    def _execute_sandbox_payment(self, intent_id):
        self._scope()
        spec = self.intents.get(intent_id)
        if spec is None:
            raise ToolError("unknown intent")
        return self.engine.execute(spec, self.session_user, approval_token=self.user_tokens.get(f"approved:{intent_id}"))

    def _read_audit_events(self, limit=20):
        self._scope()
        return [{k: e[k] for k in ("sequence", "actor", "event_type", "entity", "event_hash")}
                for e in self.engine.ledger.events[-limit:]]
