"""TuitionPilot 2.0 core tests. Test names carry the workbook QA IDs (QA_Test_Cases)."""
import json
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from tuitionpilot.treasury import fixtures as F
from tuitionpilot.treasury.beneficiary import BeneficiaryRegistry, VerificationRejected
from tuitionpilot.treasury.calendar import BusinessCalendar, latest_safe_arrival
from tuitionpilot.treasury.currency import Amount, CurrencyError, Rate
from tuitionpilot.treasury.engine import SimulatedTimeout, TreasuryEngine
from tuitionpilot.treasury.flagship import replay, run
from tuitionpilot.treasury.ledger import Ledger
from tuitionpilot.treasury.limits import Caps, LimitBook
from tuitionpilot.treasury.optimizer import optimize
from tuitionpilot.treasury.policy import Decision, PolicyEngine, PolicyInput
from tuitionpilot.treasury.quotes import RouteQuote, cost_for_target
from tuitionpilot.treasury.tools import ToolRouter

OB = "OB-TUITION-AUT26"
USER = "student-001"


def eng(**kw):
    return TreasuryEngine(**kw)


def money_input(**kw):
    base = dict(action="EXECUTE_PAYMENT", user_id=USER, session_user_id=USER, tier=3,
                amount=Amount.of("500000", "KZT"), target=Amount.of("7000", "CNY"), currency_pair=("KZT", "CNY"),
                quote_fresh=True, beneficiary_status="VERIFIED", liquidity_ok=True, kill_switch=False,
                audit_available=True, fx_share_pct=Decimal(20))
    base.update(kw)
    return PolicyInput(**base)


PE = PolicyEngine(Caps())
NOW = F.NOW


# ----------------------------------------------------------------- financial math
def test_qa001_all_in_cost_is_itemised_and_adds_up():
    q = F.quotes_at(NOW)["R1"]
    c = cost_for_target(q, Amount.of("45000", "CNY"))
    parts = c.principal_at_mid + c.fx_spread_cost + c.variable_fee + c.fixed_fee + c.intermediary_fee
    assert parts == c.all_in_known
    assert c.fixed_fee == Amount.of("1000", "KZT") and c.intermediary_fee == Amount.of("450", "KZT")
    assert c.fx_spread_cost.minor > 0 and c.variable_fee.minor > 0
    assert c.effective_rate_label == "KZT per 1 CNY, all-in"
    assert c.estimated_extras  # unknown receiving-bank charges listed separately, not summed


def test_qa002_quote_direction_inversion_is_consistent():
    r = Rate("KZT", "CNY", Decimal("0.01480"))
    inv = r.invert()
    assert inv.label == "KZT per 1 CNY"
    t = Amount.of("45000", "CNY")
    a, b = r.source_needed(t), Amount.of(t.major * inv.value, "KZT")
    assert abs(a.minor - b.minor) <= 1
    with pytest.raises(CurrencyError):
        r.source_needed(Amount.of("1", "KZT"))       # wrong side of the pair


def test_qa003_minor_units_and_rounding():
    assert Amount.of("1234.565", "KZT").minor == 123457
    assert Amount.of("1500", "JPY").minor == 1500 and str(Amount.of("1500", "JPY")) == "1,500 JPY"
    assert sum(p.minor for p in Amount.of("45000", "CNY").split([50, 25, 15, 10])) == 4500000
    with pytest.raises(TypeError):
        Amount.of(0.1, "KZT")                         # binary floats rejected
    with pytest.raises(CurrencyError):
        Amount.of("1", "KZT") + Amount.of("1", "CNY")  # no silent cross-currency sums
    with pytest.raises(CurrencyError):
        Amount.of("1", "XXX")


# ----------------------------------------------------------------- optimizer
def test_qa004_cheap_but_late_route_is_rejected():
    d = eng().plan(OB)
    r4 = next(c for c in d.candidates if c.route_id == "R4" and c.strategy == "all_at_once")
    cheapest_overall = min(d.candidates, key=lambda c: c.total.minor)
    assert cheapest_overall.route_id == "R4"           # it IS the cheapest...
    assert not r4.feasible and "deadline" in r4.failed()[0]   # ...and still rejected
    assert d.selected.route_id != "R4"


def test_qa005_no_feasible_plan_returns_infeasible_with_reasons():
    e = eng()
    liq = e.liquidity.copy()
    liq.flows = [replace(f, on=f.on + timedelta(days=10)) if f.kind == "confirmed_inflow" else f for f in liq.flows]
    d = optimize(e.optimizer_inputs(OB, liquidity=liq))
    assert d.status == "INFEASIBLE" and d.selected is None
    assert any("liquidity_reserve" in r for r in d.reasons_if_infeasible)
    assert d.to_dict()["selected"] is None


def test_qa006_reserve_breach_blocks_plan_under_stress():
    d = eng().plan(OB, rate_shock=Decimal("0.10"))
    c5 = next(c for c in d.candidates if c.route_id == "R2" and c.strategy == "equal_in_window")
    assert not c5.constraints["liquidity_reserve"]["pass"]
    p = PE.evaluate(money_input(liquidity_ok=False), NOW)
    assert p.decision == Decision.DENY and p.matched_rule_ids == ["POL-016"]


def test_qa007_future_rates_are_labelled_assumptions_and_savings_not_guaranteed():
    d = eng().plan(OB)
    assert any("scenarios, not knowledge" in a for a in d.assumptions)
    assert d.savings["label"].startswith("SIMULATED") and "not a real or guaranteed" in d.savings["label"]
    assert d.to_dict()["label"] == "SIMULATION — NO REAL MONEY MOVES"


def test_optimizer_ranks_feasible_by_known_cost_only():
    d = eng().plan(OB)
    feas = [c for c in d.candidates if c.feasible]
    assert d.selected.total.minor == min(c.total.minor for c in feas)
    assert d.savings["comparable"] is True and Decimal(d.savings["savings_pct"]) > 0


# ----------------------------------------------------------------- fraud / AI security
def test_qa008_changed_beneficiary_blocks_payment_pending_verification():
    e = eng()
    d = e.plan(OB)
    inv = e.parse_invoice(F.CHANGED_INVOICE_CLEAN)
    assert inv["shield"]["status"] == "MISMATCH_BLOCKED" and "account" in inv["shield"]["mismatched_fields"]
    res = e.execute(e.intent_from_invoice(inv["invoice_id"], OB, d), USER)
    assert res["state"] == "NOT_EXECUTED" and res["matched_rule_ids"] == ["POL-010"]
    # even after the student switches to the new version, the 48h hold applies
    new_v = e.registry.history("BEN-SDU")[-1]
    e.obligations[OB] = replace(e.obligations[OB], beneficiary_version_id=new_v.version_id)
    res2 = e.execute(replace(e.intent_from_plan(OB, d), beneficiary_version_id=new_v.version_id), USER)
    assert res2["matched_rule_ids"] == ["POL-010", "POL-011"]
    with pytest.raises(VerificationRejected):
        e.registry.verify(new_v.version_id, "trusted_callback", "invoice", USER, e.clock.now())
    assert e.executions() == []


def test_qa009_invoice_prompt_injection_has_no_effect_on_protected_state():
    e = eng()
    caps_before, tier_before = json.dumps(e.caps.to_dict()), e.tier
    d = e.plan(OB)
    inv = e.parse_invoice(F.CHANGED_INVOICE)
    assert "PROMPT_INJECTION" in inv["flags"] and inv["treated_as"] == "untrusted data"
    res = e.execute(e.intent_from_invoice(inv["invoice_id"], OB, d), USER)
    assert res["decision"] == "DENY" and "POL-018" in res["matched_rule_ids"]
    assert json.dumps(e.caps.to_dict()) == caps_before and e.tier == tier_before and not e.kill_switch
    assert e.executions() == [] and e.ledger.find("policy_decision")


# ----------------------------------------------------------------- policy tiers
def test_qa010_l0_cannot_execute():
    r = PE.evaluate(money_input(tier=0), NOW)
    assert r.decision == Decision.DENY and r.matched_rule_ids == ["POL-002"]


def test_qa011_l1_cannot_execute():
    r = PE.evaluate(money_input(tier=1), NOW)
    assert r.decision == Decision.DENY and r.matched_rule_ids == ["POL-003"]


def test_qa012_l2_without_approval_asks_and_does_not_execute():
    r = PE.evaluate(money_input(tier=2), NOW)
    assert r.decision == Decision.ASK and "POL-004" in r.matched_rule_ids and r.required_approval
    e = eng(tier=2)
    spec = e.intent_from_plan(OB, e.plan(OB))
    assert e.execute(spec, USER)["state"] == "NOT_EXECUTED" and e.executions() == []


def test_policy_l3_within_mandate_allows_and_above_cap_asks():
    assert PE.evaluate(money_input(), NOW).decision == Decision.ALLOW
    r = PE.evaluate(money_input(amount=Amount.of("2000000", "KZT")), NOW)
    assert r.decision == Decision.ASK and "POL-005" in r.matched_rule_ids
    r = PE.evaluate(money_input(amount=Amount.of("8000000", "KZT")), NOW)
    assert r.decision == Decision.DENY and r.matched_rule_ids == ["POL-006"]


def test_policy_identity_and_policy_mutation_are_denied():
    assert PE.evaluate(money_input(session_user_id="someone-else"), NOW).matched_rule_ids == ["POL-001"]
    assert PE.evaluate(money_input(user_id=None), NOW).matched_rule_ids == ["POL-001"]
    r = PE.evaluate(PolicyInput(action="CHANGE_POLICY", user_id=USER, session_user_id=USER, tier=3), NOW)
    assert r.decision == Decision.DENY and r.matched_rule_ids == ["POL-023"]


def test_policy_fails_closed_on_unknown_state_and_currency_pair():
    r = PE.evaluate(money_input(quote_fresh=None), NOW)
    assert r.decision == Decision.DENY and r.matched_rule_ids == ["POL-020"]
    r = PE.evaluate(money_input(currency_pair=("KZT", "KZT")), NOW)
    assert r.matched_rule_ids == ["POL-022"]
    r = PE.evaluate(money_input(currency_pair=("CNY", "KZT")), NOW)
    assert r.matched_rule_ids == ["POL-022"]


# ----------------------------------------------------------------- approvals
def _approved_engine():
    e = eng()
    d = e.plan(OB)
    spec = e.intent_from_plan(OB, d)
    info, token = e.request_approval(spec, USER)
    assert info["decision"] == "ASK" and token
    return e, spec, token


def test_qa013_approval_token_cannot_be_replayed():
    e, spec, token = _approved_engine()
    assert e.execute(spec, USER, approval_token=token)["state"] == "SIMULATED_SUBMITTED"
    st, _ = e.approvals.redeem(token, spec, e.clock.now())
    assert st == "INVALID:REUSED"
    again = e.execute(spec, USER, approval_token=token)
    assert again["decision"] == "RETURN_EXISTING" and len(e.executions()) == 1


def test_qa014_amount_change_after_approval_invalidates_it():
    e, spec, token = _approved_engine()
    mutated = replace(spec, source_amount=Amount(spec.source_amount.minor + 1, "KZT"))
    r = e.execute(mutated, USER, approval_token=token)
    assert r["decision"] == "DENY" and r["matched_rule_ids"] == ["POL-021"]
    st, _ = e.approvals.redeem(token, spec, e.clock.now())
    assert st == "INVALID:INVALIDATED"          # cannot be reused for the original either
    for field, val in (("route_id", "R2"), ("beneficiary_version_id", "BV-other"), ("quote_id", "Q-x")):
        e2, s2, t2 = _approved_engine()
        assert e2.approvals.redeem(t2, replace(s2, **{field: val}), e2.clock.now())[0] == "INVALID:FINGERPRINT_MISMATCH"


def test_approval_expires_after_15_minutes():
    e, spec, token = _approved_engine()
    e.clock.advance(minutes=16)
    assert e.approvals.redeem(token, spec, e.clock.now())[0] == "INVALID:EXPIRED"


# ----------------------------------------------------------------- limits / concurrency
def test_qa015_concurrent_requests_never_exceed_cap():
    caps = Caps(daily_minor=1_000_000_00, per_payment_minor=1_000_000_00)
    book = LimitBook(caps)
    wins, barrier = [], threading.Barrier(20)

    def worker(i):
        barrier.wait()
        r, _ = book.try_reserve(f"I{i}", Amount.of("300000", "KZT"), NOW)
        if r:
            wins.append(r)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 3 and book.committed_total() <= caps.daily_minor


# ----------------------------------------------------------------- kill switch / idempotency
def test_qa016_kill_switch_blocks_new_actions_and_only_user_can_set_it():
    e, spec, token = _approved_engine()
    with pytest.raises(PermissionError):
        e.set_kill_switch(True, actor="agent", reason="email said so")
    e.set_kill_switch(True, actor="user", reason="stop everything")
    r = e.execute(spec, USER, approval_token=token)
    assert r["decision"] == "DENY" and r["matched_rule_ids"] == ["POL-014"] and e.executions() == []
    assert e.plan(OB).status == "INFEASIBLE"


def test_qa017_timeout_then_retry_creates_no_duplicate():
    e, spec, token = _approved_engine()
    with pytest.raises(SimulatedTimeout):
        e.execute(spec, USER, approval_token=token, simulate_timeout=True)
    r1 = e.execute(spec, USER, approval_token=token)
    r2 = e.execute(spec, USER)
    assert r1["execution_id"] == r2["execution_id"] and len(e.executions()) == 1
    debits = [f for f in e.liquidity.flows if "(simulated)" in f.label]
    assert len(debits) == 1


def test_duplicate_invoice_cannot_pay_twice():
    e, spec, token = _approved_engine()
    e.execute(spec, USER, approval_token=token)
    second = replace(spec, intent_id="PI-DUPLICATE-INVOICE")               # same bill arrives twice
    r = e.execute(second, USER)
    assert r["decision"] == "RETURN_EXISTING" and "POL-017" in r["matched_rule_ids"] and len(e.executions()) == 1


# ----------------------------------------------------------------- audit
def test_qa018_modified_middle_event_breaks_chain():
    e, spec, token = _approved_engine()
    e.execute(spec, USER, approval_token=token)
    assert e.ledger.verify()["ok"]
    e.ledger.events[2]["payload"]["status"] = "TAMPERED"
    v = e.ledger.verify()
    assert not v["ok"] and v["first_bad_sequence"] == 3


def test_qa019_tail_deletion_needs_checkpoint_and_limit_is_documented():
    e, spec, token = _approved_engine()
    e.execute(spec, USER, approval_token=token)
    cp = e.ledger.checkpoint()
    del e.ledger.events[-2:]
    assert e.ledger.verify()["ok"]                       # chain alone cannot see tail deletion
    assert not e.ledger.verify_against_checkpoint(cp)["ok"]
    import tuitionpilot.treasury.ledger as L
    assert "does NOT detect on its own" in L.__doc__


def test_qa020_audit_outage_fails_closed():
    e, spec, token = _approved_engine()
    e.ledger.available = False
    r = e.execute(spec, USER, approval_token=token)
    assert r["decision"] == "DENY" and r["matched_rule_ids"] == ["POL-019"]
    assert e.executions() == [] and e.limits.committed_total() == 0


# ----------------------------------------------------------------- quotes / privacy / dates
def test_qa021_stale_quote_denied():
    d = eng().plan(OB)
    r5 = next(c for c in d.candidates if c.route_id == "R5" and c.strategy == "all_at_once")
    assert not r5.constraints["quote_freshness"]["pass"]
    e, spec, token = _approved_engine()
    e.clock.advance(minutes=31)                          # quote TTL 30 min passes
    r = e.execute(spec, USER, approval_token=token)
    assert r["decision"] == "DENY" and r["matched_rule_ids"] == ["POL-015"]


def test_qa022_no_raw_token_or_account_in_logs_or_tool_output():
    e = eng()
    tokens = {}
    router = ToolRouter(e, USER, tokens)
    out = router.run_turn([{"tool": "propose_payment_plan", "args": {"obligation_id": OB}}])
    iid = out[0]["result"]["intent"]["intent_id"]
    out2 = router.run_turn([{"tool": "request_approval", "args": {"intent_id": iid}}])
    raw = tokens[iid]
    blob = e.ledger.to_jsonl() + json.dumps(out + out2, default=str)
    assert raw not in blob
    assert "CN62000011112222" not in blob.replace(" ", "")
    assert "1111 2222" not in blob


def test_qa023_weekend_and_cutoff_are_conservative():
    cal = BusinessCalendar(tz="Asia/Almaty", holidays=frozenset({date(2026, 8, 31)}))
    sat = datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc)           # Saturday
    assert cal.effective_execution_date(sat) == date(2026, 8, 3)
    late = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)        # 17:00 Almaty, after cut-off
    assert cal.effective_execution_date(late) == date(2026, 8, 18)
    assert latest_safe_arrival(date(2026, 9, 3), 5, cal) == date(2026, 8, 26)   # holiday pulls it earlier
    assert latest_safe_arrival(date(2026, 9, 3), 5, BusinessCalendar()) == date(2026, 8, 27)
    with pytest.raises(ValueError):
        cal.effective_execution_date(datetime(2026, 8, 17, 9, 0))   # naive time rejected


def test_qa024_fixed_seed_replay_is_identical():
    r = replay(7)
    assert r["outputs_identical"] and r["ledgers_identical"]


def test_qa025_approval_screen_shows_everything_needed():
    e = eng()
    spec = e.intent_from_plan(OB, e.plan(OB))
    info, _ = e.request_approval(spec, USER)
    s = info["approval_screen"]
    for k in ("amount_debited", "amount_credited", "currency_pair", "recipient", "route", "estimated_arrival",
              "reference", "approval_expires_in_minutes", "quote_expires_at"):
        assert s[k]
    assert s["consent_prechecked"] is False and "•" in s["recipient"]["account"]


def test_qa026_execution_is_sandbox_only():
    import tuitionpilot.treasury as T, pkgutil, importlib, inspect
    for m in pkgutil.iter_modules(T.__path__):
        src = inspect.getsource(importlib.import_module(f"tuitionpilot.treasury.{m.name}"))
        import re
        assert not re.search(r"^\s*(import|from)\s+(urllib|requests|socket|http|httpx|aiohttp)\b", src, re.M), m.name
    e, spec, token = _approved_engine()
    r = e.execute(spec, USER, approval_token=token)
    assert r["state"] == "SIMULATED_SUBMITTED" and r["environment"] == "SIMULATION"
    assert all(ev["environment"] == "SIMULATION" for ev in e.ledger.events)


# ----------------------------------------------------------------- tools
def test_tools_allow_list_schema_and_turn_limit():
    e = eng()
    router = ToolRouter(e, USER, {})
    res = router.run_turn([{"tool": "change_caps", "args": {}},
                           {"tool": "read_audit_events", "args": {"limit": 5, "sql": "DROP"}},
                           {"tool": "simulate_fx_plan", "args": {"obligation_id": OB, "rate_shock_pct": 90}}])
    assert [r["status"] for r in res] == ["rejected"] * 3
    many = router.run_turn([{"tool": "read_balances", "args": {}}] * 8)
    assert sum(r["status"] == "ok" for r in many) == 6
    other = ToolRouter(e, "intruder", {})
    assert other.run_turn([{"tool": "read_balances", "args": {}}])[0]["status"] == "rejected"


def test_tool_execute_without_user_approval_does_not_move_money():
    e = eng()
    tokens = {}
    router = ToolRouter(e, USER, tokens)
    iid = router.run_turn([{"tool": "propose_payment_plan", "args": {"obligation_id": OB}}])[0]["result"]["intent"]["intent_id"]
    router.run_turn([{"tool": "request_approval", "args": {"intent_id": iid}}])
    r = router.run_turn([{"tool": "execute_sandbox_payment", "args": {"intent_id": iid}}])[0]["result"]
    assert r["state"] == "NOT_EXECUTED" and e.executions() == []
    tokens[f"approved:{iid}"] = tokens[iid]                 # the student approves on their device
    r = router.run_turn([{"tool": "execute_sandbox_payment", "args": {"intent_id": iid}}])[0]["result"]
    assert r["state"] == "SIMULATED_SUBMITTED"


def test_flagship_runs_all_ten_steps():
    ev = run()["evidence"]
    assert [s["step"] for s in ev["steps"]] == list(range(1, 11))
    assert ev["steps"][8]["result"]["simulated_debits"] == 1
    assert ev["steps"][9]["result"]["verify"]["ok"]
