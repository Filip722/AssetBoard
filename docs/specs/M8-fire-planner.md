# M8 — FIRE planner

**Goal:** a year-by-year financial-independence projection that mirrors your spreadsheet's model, then improves it: named scenarios, inputs linked to real data, one-off events, sustainability check, comparison and sensitivity.

**Exit criteria:** the projection engine reproduces the spreadsheet's results for the same inputs (golden test); scenarios can pull starting portfolio, expenses and income from the app's own data; two or three scenarios can be compared; the dashboard (M9) can show FIRE progress.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Model of the source spreadsheet (to be mirrored exactly in "parity" behaviour)**
Inputs: inflation `i`; annual investment return above inflation `r`; annual income growth `g`; "expected FIRE age" = the age at which **income stops**; birth year (embedded in formulas); starting income; starting expenses (today's money); starting portfolio (investable assets); explicit yearly net-savings values for the first years.

Rows per year `t` (`t = 0` is the current year):
```
age_t      = year_t - birth_year
income_t   = start_income                              (t = 0)
           = income_{t-1} * (1 + g)  if age_t < stop_age else 0   (t ≥ 1)
expenses_t = start_expenses                            (t = 0)
           = expenses_{t-1} * (1 + i)                  (t ≥ 1)
net_t      = start_portfolio                           (t = 0, column reused as the start value)
           = override_t if present else income_t - expenses_t     (t ≥ 1)
gain_t     = (t = 0: net_0 ; t ≥ 1: networth_{t-1}) * (i + r)
networth_t = net_0 + gain_0                            (t = 0)
           = networth_{t-1} + net_t + gain_t           (t ≥ 1)
```
Computed outputs: **FIRE age/year** = first row where `expenses_t < gain_t` (returns cover inflated expenses); **% achieved (sheet definition)** = `networth_0 / networth_at_FIRE_row`.

**Data model (sketch)**
- `FireScenario(id, workspace_id, name, birth_year, start_year, inflation_bp, real_return_bp, income_growth_bp, stop_working_age, horizon_age (default 95), rule[returns_cover_expenses|swr], swr_bp?, income_mode[manual|trailing_12m], income_manual?, expenses_mode[manual|trailing_12m], expenses_manual?, portfolio_mode[manual|linked_investable], portfolio_manual?, is_default, archived_at)`
- `FireContribution(scenario_id, year, net_amount Money)` — explicit yearly net-savings overrides.
- `FireEvent(scenario_id, year, kind[cash|expense_change|income_change], amount Money, label)` — `cash`: one-off added to net worth in that year; `expense_change` / `income_change`: annual change from that year onward, entered in today's money and inflated/grown like the base series.

---

## F8.1 Scenario model and settings

**Acceptance**
- Scenario CRUD with all fields above; duplicate scenario; mark one as default.
- Rates are entered as percentages and stored as basis points. Birth year is prefilled from the user profile (optional profile field `birth_year`, added here).
- Contribution overrides editor (table: year → amount, add a row or paste a column) and events editor.
- Validation: horizon_age > current age; stop_working_age ≥ current age; rates within sane ranges (confirm outside).
- Plain-language tooltips explain each input; a notice states that projections are illustrative, not advice.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.1 from docs/specs/M8-fire-planner.md: FireScenario, FireContribution, FireEvent models + migration, optional profile birth_year,
CRUD/duplicate/default UI with contribution and event editors, validation and tooltips (translatable). No projection logic yet.
```

---

## F8.2 Projection engine (pure domain)

**Acceptance**
- `project(inputs) -> Projection` in `domain/fire.py`; no I/O. Rows contain nominal and **real (today's money)** values for income, expenses, net, gain, net worth.
- **Parity mode** (default for the `returns_cover_expenses` rule) follows the formulas in this spec exactly, including the `t = 0` convention.
- Extensions on top, all optional and neutral when unused: contribution overrides; events (`cash`, `expense_change`, `income_change`); rule `swr` (FIRE when `networth_{t-1} × swr ≥ expenses_t`); continuation until `horizon_age`.
- Metrics: `fire_year`, `fire_age` (or `None`), `networth_at_fire` (nominal and real), `fire_number_today` (real: annual expenses ÷ `swr`, or ÷ `(i + r)`-equivalent for the sheet rule, documented), `progress_sheet`, `progress_today = start_portfolio / fire_number_today`, `years_to_fire`, `depletion_age` (first age with net worth < 0 after income stops, else `None`), `min_networth_after_stop`.
- **Golden test:** an independent naive loop in the test module, transcribed from the spreadsheet formulas above, matches the engine exactly (every row, FIRE age, % achieved) for at least three input sets, including one where FIRE is never reached and one with overrides for the first ten years.
- Property tests: with zero return and zero inflation, net worth equals start + cumulative net; with events absent, results equal parity mode; real values equal nominal ÷ (1+i)^t.
- Uses integer minor units internally; a single rounding step per row (documented), or `Decimal` with explicit quantization — state the choice in an ADR.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.2 from docs/specs/M8-fire-planner.md: pure project() with parity mode exactly as the formulas in the spec, plus overrides, events,
the swr rule, horizon continuation, and all listed metrics (nominal and real). Write a short ADR on numeric representation/rounding.
Golden test: independent naive loop in the tests; plus the property tests. No UI.
```

---

## F8.3 Scenario page: table, chart, key metrics

**Acceptance**
- Key metrics header: FIRE age/year, years to FIRE, FIRE number (today's money), progress bar (sheet definition and today's-money definition shown together with a tooltip explaining the difference), depletion age warning if any.
- Year-by-year table (like the spreadsheet) with a nominal/real toggle; FIRE row highlighted; rows after income stops styled differently; CSV export.
- Chart: net worth over time with the FIRE point marked; second chart: expenses vs investment gain lines showing the crossing.
- Changing any input re-renders results instantly (HTMX; no page reload) with debounce.
- Empty/never-reached states are explained ("With these inputs returns never cover expenses before age 95").

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.3 from docs/specs/M8-fire-planner.md: scenario results page with key metrics, nominal/real table toggle, CSV export, two ECharts charts,
live recalculation via HTMX with debounce. The UI calls the service that calls project(); no math in templates or JS.
```

---

## F8.4 Inputs linked to real data

**Acceptance**
- `portfolio_mode=linked_investable`: starting portfolio = value of investable portfolio classes (M6, look-through) + net-worth items flagged `investable` (e.g., cash savings), in base currency, as of today. Shows the breakdown and a link to each source.
- `expenses_mode=trailing_12m`: last 12 **full** months of expense transactions with `treatment=regular` (savings and transfers excluded), shown with per-category breakdown; one-off "unusual" transactions can be excluded by the user via a per-scenario exclusion list of categories.
- `income_mode=trailing_12m`: earned income over the same window (regular treatment). Fewer than 12 months of data → use available months scaled, with a visible warning.
- **Loan payoff suggestion:** for each loan (M7) show "Payoff in <year> frees <amount>/year (today's money)"; one click creates an `expense_change` event (negative expense change) for that year.
- Linked values display "as of" timestamps and never silently fall back to zero: missing data → blocking message in the scenario header.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.4 from docs/specs/M8-fire-planner.md: services that compute linked starting portfolio, trailing-12-month expenses/income (full months,
treatment=regular, category exclusions), and loan-payoff event suggestions; wire the *_mode fields in the scenario form and results.
Tests with the fixed Clock: window boundaries (full months only), savings/transfer exclusion, fewer-than-12-months handling, linked portfolio equals M6 valuation.
```

---

## F8.5 Scenario comparison and sensitivity

**Acceptance**
- Compare up to 3 scenarios: table (FIRE age/year, net worth at FIRE real, depletion age, years to FIRE) and overlay chart of net worth.
- **Sensitivity grid** for one scenario: pure `sensitivity(inputs, return_deltas, expense_factors) -> grid` of FIRE age; default axes: real return −2…+2 pp in 1 pp steps × expenses −20%…+20% in 10% steps; cells show age or "—" (never); heat-map styling.
- Tests: the base cell equals the scenario's FIRE age; monotonicity (higher return never delays FIRE; higher expenses never accelerate it) as property tests.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.5 from docs/specs/M8-fire-planner.md: multi-scenario comparison page and a pure sensitivity() function with the monotonicity property tests,
heat-map grid UI.
```

---

## F8.6 Importer extension: FIRE sheet

**Acceptance**
- Reads the inputs block (inflation, return above inflation, income growth, expected FIRE age) and starting values (income, expenses) from cached cells; **detects the birth year inside formulas** (e.g., a constant subtracted from the year column) by reading formula text; reads the explicit yearly net-savings values that precede the formula-driven rows and offers them as `FireContribution`s.
- Creates one scenario "Imported from workbook" via a prefilled F8.1 form for confirmation (nothing is created without the user pressing confirm).
- Parity check: after creation, run `project()` and compare FIRE age and net worth by year with the workbook's cached values; report differences.
- Extend `scripts/make_sample_workbook.py` with a synthetic FIRE sheet containing these quirks.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.6 from docs/specs/M8-fire-planner.md as an extension of the M4 importer: parse the FIRE inputs and starting values, detect birth year from
formula text (two-pass read: formulas and cached values), propose contributions, prefilled scenario form, parity check against cached values.
Tests on the extended synthetic workbook.
```

---

## F8.7 (stretch) Monte Carlo and Coast FIRE

**Acceptance:** Monte Carlo with user-set mean/volatility of real returns (seeded RNG for reproducibility) showing the probability the portfolio survives to `horizon_age` and percentile bands (10/50/90) on the chart; Coast FIRE number = portfolio needed today so growth alone reaches the FIRE number by a target age; both clearly labelled as simplified models.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.7 (stretch) from docs/specs/M8-fire-planner.md: pure monte_carlo() with a seeded numpy-free RNG (or justify numpy), percentile bands,
survival probability, coast_fire_number(). Tests: determinism with the same seed; zero-volatility result equals the deterministic projection.
```
