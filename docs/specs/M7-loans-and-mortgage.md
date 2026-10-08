# M7 — Loans and mortgage

**Goal:** model a mortgage (and other debts) realistically: annuity or equal-principal installments, fixed or variable rate (reference rate + margin), insurance/fee components, monthly and one-off overpayments, payoff date, interest saved, what-if scenarios — and connect it to net worth and recurring payments.

**Exit criteria:** a loan can be defined from either its original terms or a current balance; the schedule engine reproduces your spreadsheet's behaviour (including "top up to a total monthly amount" overpayments); the loan page shows remaining principal today, payoff date, % repaid, interest saved, charts and scenarios; the loan feeds net worth and appears in Upcoming as a scheduled payment.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Observed behaviour of the source spreadsheet (to be matched)**
- Installment from original terms: `PMT(rate/12, term, -principal)`, rate = reference rate + margin.
- Monthly interest = `balance × annual_rate / 12`.
- Extra cost components each month: one **percentage of the remaining balance** (a very small percent) and one **fixed amount**.
- Overpayment is defined as a **target total monthly outflow**: `overpayment = target − principal − interest − costs` while the balance exceeds the target; when the balance is smaller, the remainder is paid off.
- Outputs: total interest, % repaid, remaining principal today (lookup by today's date), approximate payoff date.
- The schedule starts from a *current balance* rather than from origination.

**Data model (sketch)**
- `Loan(id, workspace_id, name, kind[annuity|equal_principal|installment_plan], principal_original Money?, origination_date?, first_payment_date, term_months, payment_day, rate_mode[fixed|variable], margin_bp?, reference_rate_id?, day_count[monthly|actual_365], installment_override Money?, overpayment_mode[none|fixed_extra|target_total], overpayment_amount Money?, overpayment_strategy[reduce_term|reduce_installment], property_instrument_id?, scheduled_item_id?, archived_at)`
- `LoanRateChange(loan_id, effective_from, rate_bp)` — for `rate_mode=fixed` (rate changes over time).
- `ReferenceRate(id, workspace_id, name, notes)`; `ReferenceRateValue(reference_rate_id, effective_from, value_bp)` — user-defined series (WIBOR 1M, WIRON, EURIBOR… the app hardcodes none).
- `LoanCostComponent(loan_id, name, mode[percent|fixed], rate_bp?/fixed Money?, basis[opening_balance], effective_from, effective_to?)` — uses `PercentOrFixed`; percent is applied monthly to the opening balance.
- `LoanOverpayment(loan_id, date, amount Money, note)` — one-off planned/actual overpayments.
- `LoanPayment(loan_id, date, kind[installment|overpayment], total, principal, interest, costs, note)` — actual payments (optional).
- `LoanBalanceCheckpoint(loan_id, as_of, remaining_principal Money, note)` — bank-confirmed balances.
- `InstallmentPlan(loan_id, installment Money, remaining_count, end_date)` — only for `kind=installment_plan`.

---

## F7.1 Loans: terms and configuration

**Story:** I define my mortgage once, however much I know about it.

**Acceptance**
- Loan wizard with two entry paths: **"I know the original terms"** (principal, origination date, term, first payment date) and **"I only know my current balance"** (balance, date, remaining months → creates a checkpoint; `principal_original` optional but needed for % repaid).
- Rate configuration: fixed with an editable list of rate changes, or variable = reference rate + margin; reference rates managed on their own page (add a value with an effective date).
- Cost components (percent of opening balance per month and/or fixed amount per month) with effective dates.
- Overpayment settings: none / fixed extra amount per month / **target total monthly amount**; strategy `reduce_term` (default) or `reduce_installment`; one-off overpayments list.
- Optional `installment_override` for when the bank's actual installment differs from the computed one.
- Validation: term > 0, margin/rates in a sane range with a confirmation for rates above 25%, overpayment target not below installment.
- Editors/owners only.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.1 from docs/specs/M7-loans-and-mortgage.md: Loan, LoanRateChange, ReferenceRate(+Value), LoanCostComponent, LoanOverpayment,
LoanBalanceCheckpoint models + migration (reuse the PercentOrFixed value object from M3), services, and the two-path wizard + edit screens.
No schedule computation yet. Tests for validation rules and workspace scoping.
```

---

## F7.2 Schedule engine (pure domain)

**Acceptance**
- `build_schedule(terms, rate_timeline, costs, overpayments, anchor, as_of, strategy, day_count) -> Schedule` in `domain/loans.py`; no I/O, no clock.
- Rows: `n, date, annual_rate_bp, opening, interest, principal, overpayment, costs, installment_total, closing`.
- Start state = latest checkpoint ≤ `as_of`, otherwise origination.
- Annuity installment = `PMT(rate/12, remaining_months, opening)` unless `installment_override`; recomputed when the rate changes (and after overpayments with `reduce_installment`). Equal-principal: `principal = original / term`.
- `monthly` day count: `interest = opening × rate / 12`; `actual_365`: `opening × rate × days/365`. Rounding half-up per component per row.
- Overpayment modes: `fixed_extra` = extra on top of installment; `target_total` = `max(0, target − principal − interest − costs)` while balance exceeds the target; the last payment clears the balance exactly (no negative or tiny residual balances).
- Summary function: remaining principal at `as_of`, next installment total, total remaining interest and costs, payoff date, months remaining, % repaid.
- **Golden tests**
  1. No overpayments: schedule equals `numpy_financial.pmt/ipmt/ppmt` row by row (dev-only oracle) for a sample loan.
  2. With overpayments: an independent naive loop in the test module, transcribed from the spreadsheet behaviour listed above, matches the engine exactly (rows and payoff date).
  3. Property tests: closing balance never negative; sum of principal + overpayments = starting balance; total paid = principal + interest + costs; more overpayment never increases total interest.
- Performance: a 360-month schedule builds in < 10 ms.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.2 from docs/specs/M7-loans-and-mortgage.md: pure build_schedule() and summary functions in domain/loans.py with annuity and equal-principal,
rate changes, cost components, both overpayment modes, both strategies, both day-count conventions, exact final payment.
Money is integer minor units, half-up rounding per component. Golden tests: numpy_financial oracle (dev dependency only) and an independent naive loop in the tests;
plus the property tests listed. No UI in this feature.
```

---

## F7.3 Loan page: overview, schedule and charts

**Acceptance**
- Loan overview: remaining principal today, next payment (date + amount breakdown), current rate (with reference + margin), % repaid with progress bar, payoff date, total interest remaining/lifetime, total costs, months remaining.
- Schedule table (paged, highlights "today", shows past rows as projected-or-actual when payments exist), CSV export.
- Charts: balance over time (baseline vs with overpayments), stacked principal/interest/costs per month.
- Rate-change markers on the charts.
- All numbers come from `build_schedule()`; the UI contains no finance math.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.3 from docs/specs/M7-loans-and-mortgage.md: loan overview page, schedule table with CSV export, ECharts balance and stacked payment charts.
Service layer calls build_schedule(); templates only render. Tests: service returns the same summary as the domain function for a fixture loan.
```

---

## F7.4 Overpayment scenarios (what-if)

**Story:** I can see what happens if I overpay more, less, or if the rate changes.

**Acceptance**
- Baseline = same loan with no overpayments; "current plan" = configured overpayments; **what-if** = up to 3 user-defined variations (different overpayment mode/amount, strategy, rate shift ±X percentage points from a chosen date, one-off lump sum).
- Comparison table: payoff date, months saved vs baseline, total interest, **interest saved vs baseline**, total paid; chart overlaying balances.
- What-ifs are ephemeral by default; "save scenario" persists name + parameters (`LoanScenario` model).
- Results are deterministic and use the same engine.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.4 from docs/specs/M7-loans-and-mortgage.md: scenario parameter object, comparison service calling build_schedule() for baseline/plan/what-ifs,
comparison table + overlay chart, optional LoanScenario persistence (model + migration). Tests: interest saved equals baseline interest minus scenario interest;
identical parameters give identical results.
```

---

## F7.5 Actual payments and checkpoints

**Acceptance**
- Record actual payments (installment/overpayment) with date and amounts, or accept a single total and let the app split it per the schedule; link optionally to the ledger `Transaction`.
- Add **balance checkpoints** from bank statements; the schedule re-anchors on the latest checkpoint and shows the **variance** between the projected balance and the checkpoint at that date.
- Posting an installment occurrence from Upcoming (F7.6) can create a `LoanPayment` automatically (setting).
- Past rows with recorded payments display actual values.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.5 from docs/specs/M7-loans-and-mortgage.md: LoanPayment model + migration, payment recording UI/service, checkpoint re-anchoring and variance display,
optional ledger transaction link. Tests: re-anchoring changes the future schedule only; variance computation; actuals override projections for past rows.
```

---

## F7.6 Integrations: net worth, property LTV, scheduled payments

**Acceptance**
- Register `loan_balance` resolver (M5 framework) returning remaining principal at any `as_of` (uses recorded checkpoints/payments where available, projection otherwise); helper creates a linked liability item in the Mortgage category.
- Link a loan to a property `Instrument` (M6, real estate) → show **LTV** = remaining principal / property value, and equity = value − remaining principal.
- **Scheduled payment generation:** one click creates/maintains a `ScheduledItem`-backed series of installment occurrences (category selectable), `amount_mode=from_loan`: expected amount for each due date comes from the engine (so a rate change updates future pending occurrences automatically).
- Disabling generation removes pending future occurrences only.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.6 from docs/specs/M7-loans-and-mortgage.md: loan_balance ValuationResolver, linked liability helper, property LTV/equity display,
and generation of installment occurrences with amount_mode=from_loan (extend the M3 generator; recalculation of pending future occurrences on loan changes).
Tests: resolver values for past/future dates, occurrence amounts follow rate changes, posted occurrences untouched.
```

---

## F7.7 Installment plans (simple fixed installments)

**Story:** For 0% installment purchases and similar, I just track installment amount, remaining count and end date.

**Acceptance**
- `kind=installment_plan`: name, installment amount, remaining count (or end date), payment day, category; remaining liability = installment × remaining count (no interest modelling).
- Decrement remaining count when an installment occurrence is posted; plan closes at 0.
- Appears in net worth (linked liability, "Installments" category) and in Upcoming.
- Overview table of all installment plans: monthly total, total remaining, last end date.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.7 from docs/specs/M7-loans-and-mortgage.md: InstallmentPlan model + migration, UI, generation of occurrences via the M3 generator,
decrement-on-post behaviour, linked liability and overview table. Tests for countdown, closing at zero, and idempotent generation.
```

---

## F7.8 Importer extension: loan parameters

**Acceptance**
- Reads the loan sheet's parameter block (observed: principal, term in months, suggested overpayment/target amount, margin, current rate or reference rate cell, per-month cost parameters, first schedule row's balance) and proposes a loan via the F7.1 wizard prefilled; the user confirms.
- Schedule rows are **not** imported (recomputed by the engine).
- Validation report: after creating the loan, compare engine payoff date, total interest and remaining principal at the workbook's schedule date against the workbook's cached values; differences beyond a tolerance (default 1 month / 1%) are flagged with likely causes (rate, day-count, cost components).
- Noted source quirks are reported, not silently fixed (e.g., a missing month in the date column).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.8 from docs/specs/M7-loans-and-mortgage.md as an extension of the M4 importer: parse the loan parameter block and schedule cached values,
prefill the F7.1 wizard, run the validation comparison against build_schedule(), report date gaps in the source schedule.
Extend scripts/make_sample_workbook.py with a synthetic loan sheet (including one skipped month). Tests on it.
```
