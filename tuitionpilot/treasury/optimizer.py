"""Deadline-constrained payment optimizer.

1. Normalise the obligation: target amount/currency, due date, latest safe arrival.
2. Generate bounded, deterministic candidates: route × schedule × execution dates.
3. Evaluate hard constraints for every candidate. A failed constraint cannot be
   offset by a low price.
4. Rank only feasible candidates by known all-in cost (ties: larger deadline
   buffer, then fewer operations). No hidden weighted score.
5. Return a decision record: selected plan or INFEASIBLE with reasons, every
   rejected alternative with its reason, assumptions, quote timestamps,
   baseline comparison and policy version.

Tranches are separate transfers of part of the target amount, each with its
own fees. Future rates are unknown: later tranches are priced at today's quote
as a labelled assumption and stress-tested with explicit shocks. Tranching
reallocates timing risk; it is not hedging and usually costs more in fees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from .calendar import BusinessCalendar, instruction_time_for, latest_safe_arrival
from .currency import Amount, amounts_sum
from .ledger import POLICY_VERSION
from .limits import Caps
from .liquidity import CashFlow, LiquidityBook
from .quotes import CostBreakdown, RouteQuote, cost_for_target

STRATEGIES = ("all_at_once", "weekly_4", "front_loaded_in_window", "equal_in_window")
FRONT_WEIGHTS = (50, 25, 15, 10)


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    target: Amount
    due: date
    processing_buffer_bd: int
    beneficiary_id: str
    beneficiary_version_id: str
    reference: str

    def latest_safe(self, cal: BusinessCalendar) -> date:
        return latest_safe_arrival(self.due, self.processing_buffer_bd, cal)


@dataclass
class OptimizerInputs:
    now: datetime
    obligation: Obligation
    quotes: dict                      # route_id -> RouteQuote
    calendar: BusinessCalendar
    liquidity: LiquidityBook
    caps: Caps
    beneficiary_status: str           # VERIFIED | HOLD | UNVERIFIED | UNKNOWN | MISMATCH
    allowed_routes: set
    kill_switch: bool = False
    already_executed: bool = False
    rate_shock: Decimal = Decimal(0)  # applies to tranches executed after the first execution date
    extra_delay_bd: int = 0           # stress: added to every route's arrival
    outage: set = field(default_factory=set)
    baseline_route: str = "R1"


@dataclass
class Tranche:
    index: int
    execution_date: date
    target: Amount
    cost: CostBreakdown
    arrival_earliest: date
    arrival_latest: date
    shocked: bool

    def to_dict(self) -> dict:
        return {"index": self.index, "execution_date": self.execution_date.isoformat(),
                "target": str(self.target), "all_in_known": str(self.cost.all_in_known),
                "arrival_range": [self.arrival_earliest.isoformat(), self.arrival_latest.isoformat()],
                "priced_with_shock": self.shocked, "cost": self.cost.to_dict()}


@dataclass
class Candidate:
    candidate_id: str
    route_id: str
    route_name: str
    strategy: str
    tranches: list
    constraints: dict
    total: Amount
    buffer_bd: int
    needs_approval: list
    remaining_exposure_after_first: Amount

    @property
    def feasible(self) -> bool:
        return all(c["pass"] for c in self.constraints.values())

    def failed(self) -> list[str]:
        return [f"{k}: {v['detail']}" for k, v in self.constraints.items() if not v["pass"]]

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "route_id": self.route_id, "route_name": self.route_name,
                "strategy": self.strategy, "feasible": self.feasible,
                "result": "FEASIBLE" if self.feasible else "INFEASIBLE",
                "total_all_in_known": str(self.total), "total_minor": self.total.minor,
                "deadline_buffer_bd": self.buffer_bd, "operations": len(self.tranches),
                "needs_approval": self.needs_approval,
                "remaining_fx_exposure_after_first": str(self.remaining_exposure_after_first),
                "constraints": self.constraints, "failed": self.failed(),
                "tranches": [t.to_dict() for t in self.tranches]}


def _business_days_window(cal: BusinessCalendar, start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _schedule(strategy: str, cal: BusinessCalendar, start: date, last_ok: date) -> tuple[list[date], list[int]]:
    if strategy == "all_at_once":
        return [start], [1]
    if strategy == "weekly_4":
        return [cal.roll_forward(start + timedelta(days=7 * i)) for i in range(4)], [1, 1, 1, 1]
    window = _business_days_window(cal, start, last_ok) if last_ok >= start else []
    if not window:
        window = [start]          # constraint check will reject it as late
    n = min(4, len(window))
    idx = [round(i * (len(window) - 1) / (n - 1)) for i in range(n)] if n > 1 else [0]
    dates = [window[i] for i in idx]
    if strategy == "front_loaded_in_window":
        w = list(FRONT_WEIGHTS[:n])
        return dates, w
    return dates, [1] * n


def build_candidates(inp: OptimizerInputs) -> list[Candidate]:
    cal, ob = inp.calendar, inp.obligation
    safe = ob.latest_safe(cal)
    start = cal.effective_execution_date(inp.now)
    out, n = [], 0
    for rid in sorted(inp.quotes):
        q: RouteQuote = inp.quotes[rid]
        max_bd = q.route.arrival_max_bd + inp.extra_delay_bd
        min_bd = q.route.arrival_min_bd + inp.extra_delay_bd
        last_ok = cal.subtract_business_days(safe, max_bd)
        for strat in STRATEGIES:
            dates, weights = _schedule(strat, cal, start, last_ok)
            parts = ob.target.split(weights)
            tranches = []
            for i, (d, part) in enumerate(zip(dates, parts)):
                shocked = d > start and inp.rate_shock != 0
                cost = cost_for_target(q, part, inp.rate_shock if shocked else Decimal(0))
                a0, a1 = cal.arrival_range(d, min_bd, max_bd)
                tranches.append(Tranche(i, d, part, cost, a0, a1, shocked))
            n += 1
            out.append(_evaluate(f"C{n:02d}", q, strat, tranches, inp, safe))
    return out


def _evaluate(cid: str, q: RouteQuote, strat: str, tranches: list, inp: OptimizerInputs, safe: date) -> Candidate:
    cal, caps = inp.calendar, inp.caps
    total = amounts_sum((t.cost.all_in_known for t in tranches), q.source_ccy)
    latest = max(t.arrival_latest for t in tranches)
    buffer_bd = cal.business_days_between(latest, safe)
    c = {}

    late = [t for t in tranches if t.arrival_latest > safe]
    c["deadline"] = {"pass": not late, "detail": (f"latest arrival {latest.isoformat()} vs latest safe {safe.isoformat()}"
                                                  + (f"; {len(late)} tranche(s) late" if late else ""))}
    fresh = q.is_fresh(inp.now)
    c["quote_freshness"] = {"pass": fresh, "detail": f"quote age {int(q.age(inp.now).total_seconds() // 60)} min, "
                                                     f"TTL {int(q.ttl.total_seconds() // 60)} min"}
    flows = [CashFlow(t.execution_date, t.cost.all_in_known, "planned_outflow", f"{cid} tranche {t.index + 1}")
             for t in tranches]
    head, on = inp.liquidity.min_headroom(flows)
    c["liquidity_reserve"] = {"pass": head.minor >= 0,
                              "detail": f"lowest headroom above reserve {head} on {on}"}
    c["beneficiary_verified"] = {"pass": inp.beneficiary_status == "VERIFIED",
                                 "detail": f"beneficiary status {inp.beneficiary_status}"}
    route_ok = q.route.route_id in inp.allowed_routes and q.route.route_id not in inp.outage
    c["route_allowed"] = {"pass": route_ok and q.route.supports_reference,
                          "detail": ("route available and approved" if route_ok else
                                     "route in outage" if q.route.route_id in inp.outage else "route not approved by user")
                          + ("" if q.route.supports_reference else "; cannot carry payment reference")}
    c["kill_switch"] = {"pass": not inp.kill_switch, "detail": "active" if inp.kill_switch else "off"}
    over = [t for t in tranches if t.cost.all_in_known.minor > caps.per_payment_minor]
    c["hard_caps"] = {"pass": not over and total.minor <= caps.rolling_30d_minor,
                      "detail": "within per-payment and 30-day caps" if not over else f"{len(over)} tranche(s) above hard cap"}
    c["no_duplicate"] = {"pass": not inp.already_executed,
                         "detail": "obligation already has an execution" if inp.already_executed else "no prior execution"}

    needs = []
    if any(t.cost.all_in_known.minor > caps.autonomous_minor for t in tranches):
        needs.append("POL-005 above autonomous cap")
    if any(t.target.minor * 100 > caps.max_fx_share_pct * inp.obligation.target.minor for t in tranches):
        needs.append("POL-009 FX share above limit")
    first = tranches[0].target
    remaining = Amount(inp.obligation.target.minor - first.minor, first.ccy)
    return Candidate(cid, q.route.route_id, q.route.name, strat, tranches, c, total, buffer_bd, needs, remaining)


@dataclass
class PlanDecision:
    status: str
    selected: Candidate | None
    candidates: list
    baseline: Candidate | None
    savings: dict | None
    assumptions: list
    reasons_if_infeasible: list
    latest_safe_arrival: date
    effective_start: date
    quotes: list
    policy_version: str = POLICY_VERSION

    def rejected(self) -> list[dict]:
        out = []
        for c in self.candidates:
            if self.selected and c.candidate_id == self.selected.candidate_id:
                continue
            if not c.feasible:
                out.append({"candidate_id": c.candidate_id, "route_id": c.route_id, "strategy": c.strategy,
                            "why": "INFEASIBLE: " + "; ".join(c.failed())})
            else:
                diff = Amount(c.total.minor - self.selected.total.minor, c.total.ccy) if self.selected else None
                out.append({"candidate_id": c.candidate_id, "route_id": c.route_id, "strategy": c.strategy,
                            "why": f"feasible but costs {diff} more" if diff and diff.minor > 0 else "feasible, ranked lower on tie-breakers"})
        return out

    def to_dict(self) -> dict:
        return {"status": self.status, "policy_version": self.policy_version,
                "latest_safe_arrival": self.latest_safe_arrival.isoformat(),
                "effective_start": self.effective_start.isoformat(),
                "selected": self.selected.to_dict() if self.selected else None,
                "baseline": self.baseline.to_dict() if self.baseline else None,
                "savings": self.savings, "assumptions": self.assumptions,
                "reasons_if_infeasible": self.reasons_if_infeasible,
                "rejected": self.rejected(), "candidates": [c.to_dict() for c in self.candidates],
                "quotes": self.quotes, "label": "SIMULATION — NO REAL MONEY MOVES"}


ASSUMPTIONS = [
    "All quotes, fees, routes and the payee are SIMULATED fixtures, not live data.",
    "Later tranches are priced at today's quote; each needs a fresh quote at execution (POL-015). Future rates are scenarios, not knowledge.",
    "Each tranche is a separate transfer with its own fees; the university is assumed to accept part-payments (TBD).",
    "Arrival uses the conservative end of each route's business-day range; holidays list is empty (TBD per corridor).",
    "Receiving-bank charges are unknown and not included in the known all-in cost.",
    "Ranking = lowest known all-in cost among feasible plans; deadline buffer and operations shown separately, not blended.",
]


def optimize(inp: OptimizerInputs) -> PlanDecision:
    cands = build_candidates(inp)
    feas = sorted([c for c in cands if c.feasible], key=lambda c: (c.total.minor, -c.buffer_bd, len(c.tranches)))
    selected = feas[0] if feas else None
    base = next((c for c in cands if c.route_id == inp.baseline_route and c.strategy == "all_at_once"), None)
    savings = None
    if selected and base:
        if base.feasible:
            s = Amount(base.total.minor - selected.total.minor, base.total.ccy)
            savings = {"baseline": f"{base.route_name}, all at once", "baseline_cost": str(base.total),
                       "selected_cost": str(selected.total), "savings": str(s),
                       "savings_pct": f"{(Decimal(s.minor) / Decimal(base.total.minor) * 100):.2f}",
                       "comparable": True,
                       "basis": "same target amount, both arrive by latest safe date, both priced from fixtures quoted at "
                                + inp.quotes[inp.baseline_route].quoted_at.isoformat(),
                       "label": "SIMULATED — not a real or guaranteed saving"}
        else:
            savings = {"comparable": False, "note": "baseline route is infeasible here, so no savings figure is claimed",
                       "baseline_failed": base.failed()}
    reasons = []
    if not selected:
        agg = {}
        for c in cands:
            for k, v in c.constraints.items():
                if not v["pass"]:
                    agg.setdefault(k, 0)
                    agg[k] += 1
        reasons = [f"{k} failed in {n} of {len(cands)} candidates" for k, n in sorted(agg.items(), key=lambda x: -x[1])]
    return PlanDecision("FEASIBLE" if selected else "INFEASIBLE", selected, cands, base, savings, ASSUMPTIONS,
                        reasons, inp.obligation.latest_safe(inp.calendar), inp.calendar.effective_execution_date(inp.now),
                        [q.to_dict(inp.now) for q in inp.quotes.values()])
