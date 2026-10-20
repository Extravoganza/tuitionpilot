# ADR-002: Fixture clock, dates and units for the 2.0 sandbox

**Status:** accepted, 17 Oct 2026.

## Conflicts found in the workbook

1. `Assumptions` uses due date 2026-09-03 and latest safe arrival 2026-08-27, while `Route_Comparison` quote timestamps are 2026-10-17 12:00 UTC, after the due date.
2. `Optimizer_Candidates` plans execution on 2026-08-01. That is a Saturday, and from that date the "cheap but slow" route R4 (7–10 business days) would arrive by 2026-08-17, well before the latest safe arrival. So R4 would be **feasible and the cheapest**, the opposite of its label "Reject if late".
3. `Assumptions` has one source balance of 5,000,000 KZT and a minimum reserve of 500,000 KZT, but no outflows. Candidate C5 "reserve breached under stress" cannot happen without other commitments.

## Decisions

* **Sandbox clock:** "now" = Monday 2026-08-17 09:00 Asia/Almaty (03:00 UTC). From this date R4's conservative arrival (10 business days, 2026-08-31) is after the latest safe arrival, matching the workbook's intent. The Aug 1 start is kept as a **counterfactual** in the stress lab, showing that R4 becomes feasible.
* **Quote timestamps:** fixture quotes are re-based to the sandbox clock. R1–R4 are quoted at "now" and R5 24 hours earlier, preserving "fresh" vs "STALE". The quote TTL is 30 minutes (fixture; TBD per provider).
* **Liquidity fixture:** the 5,000,000 KZT balance is split into opening cash 4,400,000 KZT plus a confirmed sponsor inflow of 600,000 KZT on 2026-08-18. A committed living-cost outflow of 1,200,000 KZT falls on 2026-08-24. With these, C5 (four tranches on R2 under a +10% FX shock) breaches the reserve, as the workbook expects. All values are labelled SIMULATED.
* **Mid rate:** 0.01480 CNY per 1 KZT at "now", SIMULATED fixture. The direction is stored explicitly.
* **Units:** workbook fee columns "source minor units" are read as KZT tiyn (1 KZT = 100 minor units), so R1 fixed fee 100000 = 1,000.00 KZT.
* **Fees:** "Variable fee %" is applied to the source principal. "Estimated FX spread %" lowers the rate received. Workbook values such as 0.025 are fractions (2.5%).
* **Calendar:** weekends are Saturday and Sunday. The holiday list is empty by default (TBD per corridor); tests use custom holidays. The source bank cut-off is 15:00 Asia/Almaty, a fixture.
* **Rounding:** amounts the student pays are rounded up to the minor unit; amounts received are rounded down. This is conservative for the student.
* **Caps** are defined in the source currency (KZT) to avoid cross-currency sums: autonomous ≤ 1,000,000 KZT per action; hard per-payment cap 7,500,000; daily 10,000,000; rolling 30-day 15,000,000; one FX buy ≤ 60% of the obligation.
