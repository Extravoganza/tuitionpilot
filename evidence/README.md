# Execution evidence

**SIMULATION. No real money moves.** Every file here was produced by running the code in this repository with a fixed seed.

## Latest run (19 October 2026): `run_2026-10-19/`

| File | Command | Result |
|---|---|---|
| `pytest_output.txt` | `python -m pytest -q` | 104 passed |
| `flagship_output.txt` | `python -m tuitionpilot.treasury flagship --out evidence` | 10-step flagship; ledger 18 events, chain OK |
| `china_output.txt` | `python -m tuitionpilot.treasury china --out evidence` | Corridor A READY, B MISSING_DOCUMENTS → READY with proof; lifecycle to CREDITED_SIMULATED; evidence pack VALID, INVALID after tamper; evaluation 16/16, gate PASS |
| `replay_output.txt` | `python -m tuitionpilot.treasury replay --seed 7` | outputs and ledger heads identical |
| `v01_scenarios_output.txt` | `python -m tuitionpilot demo --out logs` | v0.1 scenarios S1–S8, audit chains valid |

## Files

| File | What it shows |
|---|---|
| `pytest-junit.xml` | Machine-readable test results |
| `flagship.json`, `ledger.jsonl`, `ledger.csv`, `checkpoint.json` | Flagship decision record and hash-chained ledger with checkpoint |
| `stress.json` | Stress and counterfactual lab (FX shocks, delays, late sponsor, outages, stale quotes) |
| `china_corridors.json` | Both China corridors: profiles, sources, eligibility, deadline projection |
| `payment_lifecycle.json` | Submitted ≠ credited; reconciliation |
| `evidence_pack.json` | Evidence Pack manifest and verification |
| `evaluation_run.json` | Evaluation Lab, 16 cases, release gate |
| `js_parity.json` | Browser eligibility checker = Python on 7,776 inputs, 0 differences |
| `planner_parity.json` | Browser planner = Python optimizer on 1,500 scenarios / 30,000 plans, 0 differences |
| `replay.json` | Data recorded for the website |
| `../logs/*.audit.jsonl` | v0.1 sandbox audit logs for scenarios S1–S8, plus `backtest.json` (300 FX paths) |
