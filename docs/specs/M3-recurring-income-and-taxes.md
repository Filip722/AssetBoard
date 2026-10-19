# M3 — Recurring items, variable income, taxes and reminders

**Goal:** never forget the yearly car insurance, never retype fixed income, support income that depends on days worked, and optionally model taxes on income.

**Exit criteria:** recurring items generate upcoming occurrences you can confirm in one click; a variable income source computes expected income from logged work units with an optional payment lag; optional user-defined tax rules (percent or fixed) produce a breakdown and, if desired, planned tax-payment expenses; reminders reach you through Apprise channels.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

Implementation order: F3.1 → F3.2 → F3.3 → F3.4 → F3.5 → F3.6 → (F3.7 stretch).

**Design decisions in this milestone**
- Planned things (occurrences) and real things (transactions) are separate tables. An occurrence becomes a transaction only when confirmed (or auto-posted).
- Editing a rule only regenerates **pending** future occurrences; posted or skipped ones are never touched.
- Taxes are optional and user-defined. The app never ships tax rates as facts and shows a "not tax advice" notice on the tax screens.

**Data model (sketch)**
- `ScheduledItem(id, workspace_id, name, direction, category_id, amount_mode[fixed|estimate], amount, currency, start_date, end_date?, max_count?, freq[weekly|monthly|yearly], interval, weekdays?, month_day (1..31 | -1 last day | 'first_working_day' | 'last_working_day'), month (yearly), weekend_policy[none|previous_working_day|next_working_day], lead_days, auto_post, paused_at?, notes)`
- `ScheduledOccurrence(id, workspace_id, scheduled_item_id?, income_source_id?, parent_occurrence_id?, direction, category_id, due_date, expected_minor, currency, amount_state[estimated|confirmed], status[pending|posted|skipped], transaction_id?, label, generated_by[rule|income|tax], created_at)`; unique `(scheduled_item_id, due_date)`; check: exactly one of `scheduled_item_id` / `income_source_id` is set (tax payments additionally set `parent_occurrence_id`).
- `TaxRule(id, workspace_id, name, effect[added|withheld], base[net|gross], notes, archived_at)`; `TaxRuleVersion(tax_rule_id, effective_from, mode[percent|fixed], rate_bp?, fixed_minor?, currency?)`.
- `IncomeSource(id, workspace_id, name, category_id, kind[fixed|variable], fixed_amount?, rate?, unit_label, pay_day (1..31 | -1), pay_lag_months, expected_units_mode[working_days|fixed|average|manual], expected_units_value?, expected_units_window?, record_as[cash_received|net|take_home], auto_post, active_from, active_to?)`
- `IncomeSourceTaxRule(income_source_id, tax_rule_id, position, generate_payment, payment_category_id?, due_day, due_month_offset)`
- `WorkLog(income_source_id, work_month, units Decimal, note)`; unique `(income_source_id, work_month)`.
- `NotificationChannel(id, workspace_id, user_id, name, apprise_url_encrypted, enabled, digest_time, default_lead_days)`; `NotificationLog(id, channel_id, occurrence_id?, kind, sent_on, status, error?)`; unique `(channel_id, occurrence_id, kind, sent_on)`.

---

## F3.1 Recurrence engine and scheduled items

**Story:** I define "car insurance, yearly on 14 March, about 1 800 PLN" once and it shows up every year.

**Acceptance**
- Pure domain module `recurrence.py`: `occurrences(rule, from_date, to_date, holidays) -> list[date]`; may wrap `dateutil.rrule` (justify the dependency) but the stored model is the structured fields above.
- Supports weekly (weekdays), monthly (day 1–31, last day, first/last working day), yearly (month + day), `interval`, `start_date`, `end_date`, `max_count`.
- Short months clamp (day 31 → last day of month); yearly 29 Feb → 28 Feb in non-leap years.
- Weekend policy `previous_working_day` / `next_working_day` using workspace country holidays (python-holidays; justify the dependency); `none` leaves the date.
- Scheduled item CRUD with live preview of the next 6 due dates; pause/resume; archive.
- Generator service (idempotent): materializes occurrences for the next **13 months** (setting), never duplicates (`unique(scheduled_item_id, due_date)`), by default does not create occurrences before today unless the user ticks "include past occurrences".
- Rule edit → delete and regenerate **pending future** occurrences only.
- APScheduler job runs the generator daily at 03:00 workspace time and on every rule change.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.1 from docs/specs/M3-recurring-income-and-taxes.md: pure recurrence domain module with the listed options,
ScheduledItem and ScheduledOccurrence models + migration, CRUD UI with next-6-dates preview, idempotent generator service
(13-month horizon), APScheduler daily job, regeneration of pending future occurrences on edit.
Domain tests (table-driven + hypothesis): day-31 clamping, 29 Feb yearly, last/first working day, weekend policies with a fake holiday set,
interval/end_date/max_count, determinism. Services use the injected Clock only.
```

---

## F3.2 Upcoming inbox and occurrence workflow

**Story:** I see what's due and overdue and confirm, skip or move items in one click.

**Acceptance**
- "Upcoming" page: overdue (highlighted), next 30 days (setting), grouped by week; filters by direction/category.
- Actions per occurrence: **Mark paid/received** (dialog prefilled with expected amount and due date; editable) creates a `Transaction` (`source=recurring`) linked to the occurrence and sets `status=posted`; **Skip**; **Reschedule this one** (changes only this occurrence's date); **Edit amount** (this occurrence only); **Edit rule**.
- Undo of "mark paid" within 10 seconds deletes the created transaction and restores `pending`.
- `auto_post=true` items post automatically on the due date (job runs shortly after midnight workspace time) with `source=recurring_auto`; the transaction is clearly marked and editable.
- Posting into a closed month is blocked with a clear message (the user must reopen or reschedule).
- A posted occurrence cannot be changed by rule edits.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.2 from docs/specs/M3-recurring-income-and-taxes.md: Upcoming page, occurrence actions (mark paid -> Transaction,
skip, reschedule, edit amount), undo window, auto-post job, closed-month protection in the service layer.
Tests: posting creates exactly one linked transaction (idempotent on double submit), undo restores pending, auto-post respects the Clock,
posted occurrences are immune to rule edits.
```

---

## F3.3 Tax rules (optional modelling)

**Story:** I can define taxes or levies on my income — name, percent or fixed value — see a breakdown, and optionally have the payments planned for me.

**Concepts**
- A **tax rule** has a name (VAT, income tax, social contributions, whatever the user calls it), an **effect** and a **value** that can change over time (versions with `effective_from`).
- `effect=added`: added on top of the net amount (VAT-style); the amount is passed through to a tax office. `effect=withheld`: deducted from what the user keeps (income-tax/contribution-style).
- `base`: `net` or `gross` (`gross = net + added taxes`). `added` rules always use `net`; `withheld` rules may use either.
- Value is a `PercentOrFixed`: percent (basis points) or fixed Money per income occurrence.
- Rule version is chosen by the occurrence's pay date.

**Pure function** `calculate_taxes(net, rules, record_as) -> TaxBreakdown`
```
added_i    = round_half_up(rate_i * net)            or fixed_i
gross      = net + sum(added_i)
withheld_j = round_half_up(rate_j * (net | gross))  or fixed_j
take_home  = net - sum(withheld_j)                  # added taxes are pass-through
ledger_amount = gross (cash_received) | net (net) | take_home (take_home)
```
Which taxes get a planned payment expense (`generate_payment = true`):

| `record_as` | `added` taxes | `withheld` taxes |
|---|---|---|
| `cash_received` (gross) | payment expense | payment expense |
| `net` | no ledger effect (pass-through) | payment expense |
| `take_home` | no ledger effect | no ledger effect (already deducted) |

Payment due date = `due_day` of the month `pay_date.month + due_month_offset` (clamped to month length).

**Acceptance**
- Tax rule CRUD with versions (add a new version instead of editing history), archive; notice "Rates are entered by you; this is not tax advice."
- **Calculator preview**: enter a net amount and (optionally) pick an income source → shows the breakdown table (net, each added tax, gross, each withheld tax, take-home, ledger amount, planned payments).
- Golden test: net 23 370.00 PLN; VAT 23% added → 5 375.10; gross 28 745.10; income tax 12% withheld on net → 2 804.40; fixed levy 1 000.00 withheld → total withheld 3 804.40; take-home 19 565.60. Ledger amounts: `cash_received` 28 745.10 with payments 5 375.10 + 2 804.40 + 1 000.00; `net` 23 370.00 with payments 2 804.40 + 1 000.00; `take_home` 19 565.60 with no payments.
- **Invariant (property test):** when every rule has `generate_payment`, `ledger_amount − sum(payments) == take_home` for all three `record_as` modes and any inputs.
- Rounding is half-up per rule; amounts are `Money`; no floats.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.3 from docs/specs/M3-recurring-income-and-taxes.md: shared PercentOrFixed value object (domain, reusable later for loan costs),
TaxRule/TaxRuleVersion models + migration, pure calculate_taxes() exactly per the formulas and the record_as table in the spec,
CRUD UI with versions, and the calculator preview page. Do NOT attach rules to income sources yet (that is F3.4).
Tests: the golden example from the spec, the take-home invariant with hypothesis, rounding edge cases (half-up), fixed rules, version selection by date.
```

---

## F3.4 Income sources and work logs (variable income)

**Story:** My income depends on the days I worked. I log worked days; the app computes what I'll be paid and when, with taxes if I want them.

**Concepts**
- `kind=fixed`: a fixed net amount per pay date (salary).
- `kind=variable`: `rate × units` where units come from `WorkLog` (default unit label "day"; also hours, items, custom).
- `pay_lag_months`: pay month = work month + lag (e.g., work in August, paid in September → lag 1). Occurrence is created on the pay date.
- Expected units before logging: `working_days` (Mon–Fri minus public holidays of the workspace country in the work month), `fixed` value, `average` of the last N logged months, or `manual` (occurrence stays "estimated: 0" until logged).
- `amount_state`: `estimated` until a `WorkLog` exists for the work month, then `confirmed` (and recalculated).
- Taxes: attach any number of tax rules (ordered), with `generate_payment`, payment category and due settings per attachment; `record_as` decides the ledger amount (see F3.3 table). Without attached rules the source works as plain income.

**Acceptance**
- Income source CRUD (fixed and variable) with a live "next occurrences" preview including tax breakdown.
- Logging units for a work month (month-level number, decimals allowed e.g., 7.5) creates/updates the occurrence of the corresponding pay month; only **pending** occurrences are recalculated; posted ones are left alone.
- Generated tax-payment occurrences (`generated_by=tax`, `parent_occurrence_id` set) follow the parent: recalculated when units change, removed if the parent is skipped.
- Golden example (test): variable source, rate 1 230.00/day, lag 1, pay day 10; work log Aug-2026 = 19 days; VAT 23% added, `record_as=cash_received` → occurrence 2026-09-10 expects 28 745.10 (net 23 370.00 + VAT 5 375.10), confirmed; tax-payment occurrences for the configured rules appear in the configured months.
- Confirming (F3.2 flow) an income occurrence posts the income transaction with the ledger amount; the user may edit the actual amount (the bank amount rules).
- Edge cases covered: no log yet, log changed after posting (offers "adjust transaction" but does not auto-edit), lag 0 and lag ≥ 2, `active_to` ends generation.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.4 from docs/specs/M3-recurring-income-and-taxes.md: IncomeSource, IncomeSourceTaxRule, WorkLog models + migration,
income occurrence generation (extend the F3.1 generator) with pay lag and expected-units modes, recalculation of pending occurrences on work-log change,
generation/maintenance of child tax-payment occurrences using calculate_taxes() from F3.3, CRUD UI with live preview, work-log UI (per work month).
Use the golden example in the spec as a test. Pure computation goes into domain/, orchestration into services/.
Do not touch posted occurrences. Tests for lag 0/1/2, estimated vs confirmed, skipped parent removes children.
```

---

## F3.5 Month page integration

**Acceptance**
- Month page lists **pending occurrences of that month** in each table (visually distinct "expected" rows with a Confirm button); confirming moves them into real rows.
- Summary gets extra columns: **Expected (pending)** and **Forecast = actual + pending expected**; KPIs show "forecast spent / forecast earned" next to actuals. A toggle hides forecast columns.
- Banner: "Enter days worked for <month>" for variable income sources whose work log for the relevant work month is missing and whose pay date is within 10 days or past.
- Overdue count badge in the nav links to the Upcoming page.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.5 from docs/specs/M3-recurring-income-and-taxes.md: show pending occurrences inside the month page tables, add Expected and Forecast
columns to summarize_month (extend the domain function and its tests, keep existing behaviour for actuals unchanged), the missing-work-log banner,
and the overdue badge. Update the golden tests from F2.4 so they still pass.
```

---

## F3.6 Notifications and reminders

**Story:** I get a push/email before things are due, overdue reminders, and a prompt to log worked days.

**Acceptance**
- Notification channels per user via **Apprise URLs** (ntfy, Telegram, email, Gotify, …). URLs are encrypted at rest (Fernet key derived from `SECRET_KEY`; justify the `cryptography` dependency) and never shown in full after saving. "Send test" button.
- Daily job at the channel's `digest_time` (user timezone): (a) occurrences due within `lead_days` (item-specific, else channel default) not yet reminded; (b) overdue occurrences (max once per day, max 7 times); (c) "enter days worked for <month>" on the first day of the month (and again 3 days before an estimated income occurrence).
- Digest mode (one message per day) or per-item mode.
- Deduplication via `NotificationLog` unique key; failures logged with error, retried next run, never crash the job.
- Tests use a fake notifier; no network.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.6 from docs/specs/M3-recurring-income-and-taxes.md: NotificationChannel/NotificationLog models + migration,
Apprise integration behind a Notifier interface (fake in tests), encrypted URL storage, settings UI with test button,
daily job producing due-soon/overdue/enter-work-units messages with deduplication, digest vs per-item mode.
Translatable message templates. Tests with the fixed Clock covering dedupe, overdue cap, timezone handling.
```

---

## F3.7 (stretch) Work calendar

**Acceptance:** optional day-level logging for a variable income source: a month calendar where you click days to mark worked (value 1 by default; half days/hours editable). If any day entries exist for a work month, `units = sum(days)` and the month-level `WorkLog` is derived and read-only. Holidays and weekends are shown; "fill working days" shortcut; bulk-clear month.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.7 (stretch) from docs/specs/M3-recurring-income-and-taxes.md: WorkDay model + migration, calendar UI (HTMX),
derivation of the month WorkLog from day entries, fill-working-days shortcut. Make sure F3.4 recalculation is triggered on every change.
```

---

## F3.8 Archive extension: recurring items, income sources, taxes and work logs

**Story:** My workspace archive (M4) also backs up and restores my recurring items, income
sources, tax rules and work logs.

**Acceptance**
- New archive files (a minor format version per ADR-0016), with lineage `id` columns and
  references by id plus name as in M4: `scheduled_items.csv`, `scheduled_occurrences.csv`,
  `income_sources.csv`, `tax_rules.csv`, `tax_rule_versions.csv`, `income_source_tax_rules.csv`,
  `work_logs.csv` (and `work_days.csv` if F3.7 exists). `transactions.csv` gains optional
  `source` and `scheduled_occurrence_id` columns so posted occurrences stay linked after a
  restore.
- Restore recreates them after categories and before transactions (occurrences after
  transactions when they reference them); merge mode imports `work_logs.csv` alone (by income
  source name, never overwriting an existing month: conflict, listed); the other files are
  restore only.
- Notification channels are **not** archived (their Apprise URLs are secrets; F9.3 decides
  about secrets); they are listed in `ARCHIVE_EXCLUDED_MODELS` with that reason, notification
  logs too.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.8 from docs/specs/M3-recurring-income-and-taxes.md as an extension of the M4 archive
(docs/specs/M4-ledger-export-import.md, ADR-0016): the new files with lineage ids, export, restore order, standalone
work_logs.csv import in merge mode, exclusions with reasons, emptiness checks, and the extended round-trip test.
```
