# TuitionPilot

**An AI agent that takes part in an international student's finances: it pays tuition and rent across borders by itself, inside limits the student sets.**

2026 Shenzhen International FinTech Competition (FinTechathon), International Track.
Theme: *AI as a Financial Participant*. **Topic C: Cross-border & Student Finance Assistant.**
Team **Borderless Scholars**: Ekaterina Kulakova (lead), Dinara Khasyanova.

**Live demo: <https://tuitionpilot-site.vercel.app/>** · Code: <https://github.com/Extravoganza/tuitionpilot>

---

> **SIMULATION — NO REAL MONEY MOVES.** All rates, routes, payees and balances are fictional test data.

## TuitionPilot 2.0 core (new)

`tuitionpilot/treasury/` adds a deadline-constrained payment optimizer and the controls from the 2.0 specification. Money is held in minor units with explicit rate direction. Quotes carry a timestamp and TTL. Latest safe arrival is computed in business days with cut-off times. Hard limits are applied before price, and the result is INFEASIBLE when nothing passes. Payee versions are immutable and verified by an independent callback. Policy rules POL-001…023, approvals bound to the exact payment, concurrency-safe caps, an idempotent sandbox executor, a ledger with checkpoints, a stress lab and a reproducible 10-step flagship demo complete the core.

```bash
python -m tuitionpilot.treasury flagship --out evidence
python -m tuitionpilot.treasury replay --seed 7
```

### Country Intelligence and the China-first pack (spec v2.1)

Before price, the agent checks which payment methods are confirmed for this student, this university and this direction. `tuitionpilot/treasury/country.py` holds versioned corridor profiles with sources, scope and freshness; a corridor resolver; deterministic eligibility (UNKNOWN is never eligible, a stale critical rule blocks everything); and deadline projection with time zones and calendars. China is the first pack, with two separate corridors: abroad → China and China → abroad. All profiles are demo fixtures; public university pages are listed only as unverified research starting points. Payments are tracked so that submitted is never shown as credited, an Evidence Pack can be verified, and an Evaluation Lab runs 16 adversarial cases with a release gate.

```bash
python -m tuitionpilot.treasury china --out evidence
```

See [docs/GAP_MATRIX_2_1.md](docs/GAP_MATRIX_2_1.md), [docs/REPORT_2_0.md](docs/REPORT_2_0.md), [docs/AS-IS.md](docs/AS-IS.md) and the ADRs in `docs/adr/`.

## The problem

Every semester, millions of international students move a large sum of money across a border to a deadline. Usually this happens in one of two ways:

* **One bank wire, sent in a hurry.** The student pays hidden FX markups of about 1.5–3%, plus fixed fees, and carries the full exchange-rate risk on a single day.
* **A phishing email.** Tuition fraud with changed bank details is a well-known pattern. Universities warn about it every year, and students are an easy target because they are far from home and under time pressure.

Most "finance assistants" only *advise*, and the student still does all the work. TuitionPilot *acts*. Because it acts, the system is designed so that the agent cannot be talked into misbehaving.

## What the agent does

| Capability | How |
|---|---|
| Reads tuition, rent and insurance invoices | Strict field extraction; invoice text is treated as untrusted data |
| Compares payment routes | 5 rails (bank wire, remittance provider, education payment platform, card, local transfer), quoted on fee, FX markup, arrival date, reliability, and whether the route can carry the payment reference |
| Buys the study currency on a deadline-safe schedule | Tranches with hard dates and opportunistic early buys, spaced apart, plus a cautious mode |
| Pays on its own | Within an explicit mandate (tier L3 + amount cap); anything larger asks for a one-tap approval |
| Multi-currency budget | Runway in months, committed payments, deadline alerts |
| Stops fraud | Detects prompt injection, changed bank details, look-alike urgency and amount anomalies; quarantines the invoice |

## Security model: the model proposes, the policy disposes

```
 user / invoice / email ──► Planner (rule-based or LLM) ──► tool call (allowlist + arg validation)
                                                               │
                                                               ▼
                         Guards (injection, payee mismatch) ─► PolicyEngine (deterministic)
                                                               │  ALLOW / REQUIRE_CONFIRMATION / DENY
                                                               ▼
                                          Sandbox bank (idempotent ledger) ──► hash-chained AuditLog
```

* **Permission tiers.** L0 read-only → L1 advise → L2 confirm every money movement → L3 autonomous within caps.
* **No self-escalation.** The agent has no tool that changes its policy. A request to raise limits is always denied (rule `P0`).
* **Hard limits** apply even with confirmation: per payment, daily and 30-day limits, a cap on how much FX can be bought at once, and allow-lists of rails and destinations.
* **Step-up approval.** Approval codes are one-time, expire after 15 minutes, are bound to the exact action fingerprint, and the policy is **re-checked at execution**.
* **New payees.** Adding one always needs the student. Payments to it are held for a 48-hour cool-off unless the student has verified the payee out of band.
* **Kill switch.** It stops all execution at once and voids pending approvals.
* **Tamper-evident audit log.** It is a SHA-256 hash chain: editing or deleting any entry is detected.
* **Idempotent execution.** A retry can never pay twice.

The full list of risks is in [docs/SECURITY.md](docs/SECURITY.md); the architecture is in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Results (sandbox)

300 simulated FX paths, 45,000 CNY from KZT, 40-day deadline (`python -m tuitionpilot.backtest`):

| Metric | Value |
|---|---|
| Average saving vs. a bank wire on day one | **1.5%** |
| Average saving vs. a bank wire at the deadline | **1.3%** |
| Exchange-rate timing risk vs. one conversion at the deadline | **−59%** (std. dev. 1.84% vs 4.46%) |
| Deadline met | **100%** of paths |

We report the result as it came out. Rates are a seeded random walk, so the FX *timing* effect averages ~0%: nothing can predict a random walk. The saving comes from routing and fees, and the tranches reduce variance. Cautious users can lock the whole amount on day one.

## Quick start

Requirements: Python ≥ 3.10. The engine has **no runtime dependencies**; only the tests need `pytest`.

```bash
git clone https://github.com/Extravoganza/tuitionpilot.git
cd tuitionpilot
python -m pip install -e ".[test]"     # or: pip install pytest

python -m pytest -q                    # 104 tests
python -m tuitionpilot demo --out logs # runs 8 scenarios, writes hash-chained audit logs
python -m tuitionpilot.backtest        # Monte-Carlo evaluation
python tools/build_demo.py             # rebuilds demo/index.html from real engine runs
```

Open the live demo at <https://tuitionpilot-site.vercel.app/>, open `demo/index.html` in any browser, or deploy it to Vercel in one click (see [docs/DEPLOY.md](docs/DEPLOY.md)). It works offline and contains:

* a guided tour of the product;
* six student money tools (transfer cost comparison, deadline-safe FX planner with a 2,000-path Monte-Carlo simulation, budget and runway, hidden-fee detector, student loan planner, invoice fraud check). They use the same fee, risk and guard logic as the engine, ported to JavaScript;
* a rule simulator that mirrors `policy.py` rule for rule;
* step-by-step replays of real engine runs (`tools/record_demo.py`);
* the backtest results with the full distribution of outcomes.

### Optional: plug in an LLM planner

The default planner is deterministic and offline. To let an LLM plan tool calls:

```bash
export TUITIONPILOT_LLM=anthropic   # or: openai (any OpenAI-compatible endpoint, set OPENAI_BASE_URL)
export TUITIONPILOT_MODEL=<model-id>
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY
```

```python
from tuitionpilot.planner import LLMPlanner
from tuitionpilot.world import build_world
agent = build_world(planner=LLMPlanner())
print(agent.handle("Compare the cheapest way to pay my tuition"))
```

The LLM can only *propose* calls from the allow-list, and every call still goes through the same policy engine. The test `test_llm_planner_with_fake_transport_still_policy_gated` shows a jailbroken model asking to raise its limit and call a non-existent tool: both are refused.

## Execution evidence

`logs/` holds one audit log per scenario (JSONL, hash-chained) and `summary.json`:

| Scenario | Shows |
|---|---|
| `s1_autonomous_tuition` | Invoice → route comparison → 4 FX tranches bought autonomously → one approval → tuition paid before the deadline; rent paid autonomously |
| `s2_invoice_fraud_and_injection` | Phishing invoice with changed IBAN + prompt injection is quarantined; a forced payment and self-escalation are denied; no money moves |
| `s3_confirm_tier_and_token_safety` | L2: wrong, replayed and expired approval codes are rejected |
| `s4_kill_switch` | Nothing executes while stopped; the plan resumes afterwards |
| `s5_read_only_and_advise` | L0/L1 tier boundaries |
| `s6_limits` | Per-payment cap and daily limit |
| `s7_new_payee` | Approval + 48-hour cool-off + out-of-band verification |
| `s8_conversation` | Natural-language turns mapped to tool calls |

Verify any log yourself:

```python
import json, hashlib
prev = "0" * 64
for line in open("logs/s1_autonomous_tuition.audit.jsonl"):
    e = json.loads(line); h = e.pop("hash")
    assert e["prev_hash"] == prev
    assert hashlib.sha256(json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() == h
    prev = h
print("chain OK")
```

## Repository layout

```
tuitionpilot/
  agent.py      the agent: tools, policy gate, autonomy loop, conversation
  policy.py     permission tiers, limits, step-up approval
  guards.py     prompt-injection and invoice-fraud detection
  audit.py      hash-chained audit log
  rails.py      payment rails and route ranking
  fxplan.py     deadline-safe FX tranche planner
  bank.py       sandbox multi-currency ledger (idempotent)
  market.py     seeded FX simulator
  invoice.py    invoice field extraction
  budget.py     runway, commitments, alerts
  planner.py    rule-based and optional LLM planners
  scenarios.py  reproducible scenarios S1–S8
  backtest.py   Monte-Carlo evaluation
  web.py        JSON bridge for the demo
  treasury/     2.0 core + country.py, tracking.py, evidence.py, evaluation.py (spec v2.1)
tests/          104 tests
demo/           demo site (index.html), built by tools/build_demo.py
docs/VIDEO_SCRIPT.md  script for the demo video
docs/           technical documentation, security self-assessment
logs/           execution evidence
```

## Disclaimer

This is a competition prototype running in a sandbox. Rates are simulated, the payees are fictional, and no real money moves. Fee schedules are illustrative and do not describe any specific provider.

License: MIT.
