# M2 — Ledger core

**Goal:** replace the monthly spreadsheet tabs: categories, income and expense entries, planned values per category, and the planned / actual / difference summary.

**Exit criteria:** for any month you can enter expenses and income quickly, set planned values per category, and see the same summary your spreadsheet shows (plus savings rate). Data is workspace-scoped and role-protected.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `Category(id, workspace_id, name, name_key, kind[expense|income], treatment[regular|savings|transfer], color, sort_order, archived_at)`; unique `(workspace_id, kind, name_key)` among non-archived (partial unique index `WHERE archived_at IS NULL`, on SQLite and PostgreSQL). `name_key` is computed in Python (`domain.categories.name_key`: NFKC, Unicode casefold, whitespace collapsed) because SQLite's `lower()` folds ASCII only. `UNIQUE(workspace_id, id)` is the target of composite foreign keys from Transaction and BudgetLine. (Changed in F2.1; was `lower(name)`.)
- `Transaction(id, workspace_id, direction[expense|income], date, amount_minor, currency, category_id, description, description_key, source, created_by_user_id?, created_at, updated_at, deleted_at)`; index `(workspace_id, date)`, `(workspace_id, category_id, date)`, `(workspace_id, direction, description_key)`. (Changed in F2.2: `created_by` is `created_by_user_id`, set to NULL when the account is deleted; `description_key` is the casefolded description for autocomplete; the reserved `scheduled_occurrence_id`, `import_batch_id`, `account_id` columns are added with their composite foreign keys by M3, M4 and the accounts feature, whose tables do not exist yet.)
- `BudgetLine(id, workspace_id, month, category_id, planned_minor, currency)`; unique `(workspace_id, month, category_id)`.
- `MonthState(id, workspace_id, month, closed_at?, closed_by_user_id?)`; `month` is the first day (DATE); unique `(workspace_id, month)`. (Changed in F2.5: `id` key for the scoped repository; `closed_by_user_id` is set to NULL when the account is deleted.)

**Carried over from F1.2 (2026-10-12):** owners can change a workspace's base currency on the
settings page, which is harmless while no amounts exist. Once transactions or budget lines
exist, M2 must either block the change or convert stored amounts; decide in F2.2's plan.
**Decided in F2.2 (owner, 2026-10-14):** block it. The base currency cannot change once any
transaction or budget line exists, soft-deleted rows included; the settings page shows it locked
with the reason (`services.workspaces.BASE_CURRENCY_LOCKS`; F2.2 adds transactions, **F2.3 must
add budget lines**).

---

## F2.1 Categories

**Story:** I manage my expense and income categories, including which ones are really savings or transfers.

**Acceptance**
- CRUD for categories: name, kind, treatment, color, order (drag or up/down buttons). Archive instead of delete when the category has transactions.
- First run offers a seed set (en + pl names): expenses — Food, Eating out, Gifts, Health, Housing, Transport, Cosmetics, Hobbies, Utilities, Travel, Entertainment, Education, Taxes, Clothes, Other; savings (expense, treatment `savings`) — Savings, Investments; income — Salary, Bonus, Interest, Sale of items, Other; savings withdrawal (income, treatment `savings`).
- Changing `kind` of a category that has transactions is blocked; changing `treatment` is allowed and shows how many past transactions are affected.
- Editors/owners only; viewers read-only.

**As built (F2.1)**
- "In use" comes from `services.categories.USAGE_SOURCES`, a static list (F2.2 changed it from import-time registration; `CategoryUsage` lives in `services.category_usage`). F2.2 adds transactions: `references` counts every transaction row of the category, soft-deleted ones included; `active_transactions` counts non-deleted ones (any date; shown as "affected" on a treatment change); `active_months` are the months with non-deleted rows. **F2.3** (budget lines) adds `references` and `active_months` (planned values change closed months' totals too); **M3** (scheduled items) `references` only.
- **Treatment change and closed months (owner, F2.2):** allowed, but the edit row warns with the closed months among `active_months` ("This changes the totals of 2 closed months: 2026-08, 2026-09.", at most 12 listed); `category.updated` records `closed_months_affected` (a count).
- Deleting a category in use archives it ("Archived because it is in use"); the Delete button is shown only for unused categories, Archive/Restore always. Restoring fails while an active category of the same kind has the name; a restored category goes to the end of its list.
- "First run" means the workspace has no categories at all (archived included); nothing is seeded automatically. Seed names are stored in the language of the user who clicks, and are ordinary names afterwards.
- Reordering (up/down buttons, active categories only) is not audited; create/update/archive/restore/delete/seed are (`category.*` actions, "Categories" group in the workspace audit log).
- Colour: free choice (`<input type="color">`), stored `#rrggbb` lowercase; rendered as SVG `fill` (CSP `style-src 'self'` forbids inline styles).
- Polish names are real translations in the catalog (not fuzzy placeholders: the catalog test rejects fuzzy entries).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.1 from docs/specs/M2-ledger-core.md: Category model + migration, repository (workspace-scoped), service, routes,
templates (HTMX inline edit/reorder), seed set (en and pl names via gettext), archive-instead-of-delete rule.
Tests: service rules (kind change blocked when used), role checks, tenant isolation test still passes.
```

---

## F2.2 Transactions and fast entry

**Story:** I add expenses and income as rows on the month page, the way I type into a spreadsheet.

**Acceptance**
- Month page `/m/YYYY-MM` shows two tables (expenses, income) each with an always-present **empty entry row**: day, amount, description, category. Enter saves and focuses the next entry row; Tab moves between fields; Esc cancels.
- **Day input accepts a day number (1–31)** interpreted within the viewed month; a full date (`2026-10-07`, `07.10.2026`) is also accepted. Invalid day for the month (e.g., 31 in November) shows an error.
- Amount accepts locale formats (`12,50`, `1 234,56`); must be > 0.
- Description autocompletes from previous descriptions; picking a suggestion pre-selects its most frequent category.
- Inline edit and delete (soft delete with 10-second undo toast).
- Direction must match category kind (an income category cannot be used in the expense table).
- Closed months are read-only (F2.5). Viewers see no entry row.
- Writes create `AuditEvent`s.
- **Closed-month guard (from F2.5).** Every transaction write, inside its write transaction and
  after `require_role_now`, first calls `services.months.lock_month_writes(session, ctx)`
  **before reading any row whose date it checks**, then `ensure_date_open` (or
  `ensure_month_open`) for every month it touches, before any change: create (the new date);
  edit (the stored row's old date **and** the new date when the date moves); soft delete and undo
  (the stored date). Reading the old date before taking the lock would let a concurrent move and
  close both pass on PostgreSQL. Map `MonthClosed` to the closed-month
  message (`web.month_routes.MONTH_CLOSED_MESSAGE`); the app-wide handler answers 409 otherwise.
  Fill the month page through `templates/months/page.html`'s `month_body` block and hide the
  entry rows when `view.closed`.

**Notes:** Use HTMX row swaps; return the updated summary block via OOB swap so totals refresh without a full reload.

**As built (F2.2 part A: data and service layer; the month-page UI is part B)**
- Single currency: every transaction is in the workspace's base currency, read from the
  workspace row inside the write (after the month lock), not from the Context. The base currency
  is then locked (see "Carried over from F1.2").
- `domain.transactions`: `parse_entry_date(text, month)` accepts a day number `1`-`31` (ASCII
  digits, within the viewed month), `YYYY-M(M)-D(D)` and `D(D).M(M).YYYY`; `/` forms are refused
  (ambiguous order). Codes: `invalid_date`, `day_not_in_month`, `date_out_of_range` (outside
  1900-01..2199-12). `amount_minor(Decimal, currency)` refuses instead of rounding:
  `amount_not_positive`, `amount_too_large` (> 999 999 999 999 999 minor units),
  `too_many_decimals`. Amounts are parsed with `i18n.numbers.parse_amount` in the caller's
  locale (`invalid_amount`). Descriptions are optional, at most 200 characters, whitespace
  collapsed, without control or invisible characters (`invalid_description`,
  `description_too_long`).
- **Product decisions (owner, 2026-10-14):** a full date outside the viewed month is allowed and
  saved to its own month (both months guarded); undo is allowed to any editor or owner, exactly
  10 seconds after `deleted_at` by the injected clock (then `UndoExpired`); audit data contains
  the amount but never the description (audit rows are append-only, so free text there could
  never be erased); month tables are ordered by date ascending, then entry order.
- Service (`services.transactions`): `create_transaction`, `update_transaction` (date, amount,
  description, category; not the direction), `delete_transaction` (soft delete; returns
  `undo_until`), `undo_delete`, `get_transaction`, `list_month` (expense and income, non-deleted),
  `suggest_descriptions`. `InvalidTransaction.errors` maps every invalid field (`direction`,
  `day`, `amount`, `description`, `category`) to its code at once. Category codes:
  `category_required`, `invalid_category` (unknown or another workspace's),
  `category_kind_mismatch` (direction must equal the category's kind), `category_archived` (an
  archived category takes no new transactions; a row keeps the archived category it has).
- Deleting a deleted row and undoing a live row change nothing and record nothing; a deleted row
  is `NotFound` for edit. If the month was closed after the deletion, undo raises `MonthClosed`
  and the row stays deleted (the window is checked first).
- Autocomplete: prefix match on `description_key` (NFKC + Unicode casefold, so "ż" finds
  "Żabka"; `%` and `_` are literal), per direction, non-deleted rows only, at most 8
  suggestions. Keys are ranked in SQL over every matching row (most used, then most recently
  dated, then the key); the spellings and categories are then read for those keys only. The shown spelling is the most used
  one; the suggested category is the most used **active** category (ties: most recently dated,
  then category order), or none.
- Audit: `transaction.created`, `.updated`, `.deleted`, `.restored` with `date`, `direction`,
  `category` (name), `amount` (plain decimal), `currency`; updates add `fields` and
  `previous_date` / `previous_amount` when those change. Group "Transactions" in the workspace
  audit log.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.2 from docs/specs/M2-ledger-core.md: Transaction model + migration, repository, service (create/update/soft-delete/undo),
month page with two tables and spreadsheet-style entry rows (HTMX), day-number date entry, locale-aware amount parsing,
description autocomplete with category suggestion, audit events.
Money must be Money/minor units only. Tests: day-number parsing incl. month-length edge cases, amount parsing, direction/kind validation,
undo within and after the window, viewer cannot write.
```

---

## F2.3 Planned values (budget lines)

**Story:** I set the planned amount per category for a month and don't re-type it every month.

**Acceptance**
- On the month page, the summary's *planned* cells are editable inline for editors.
- `BudgetLine` is upserted per (month, category). Zero/empty removes the line.
- When a month has no budget lines at all and the previous month has some, show a banner "Copy plan from <previous month>" (one click copies all lines). Copying never overwrites existing lines.
- Setting for the workspace: auto-copy previous plan when a month is first opened (default off).
- Closed months: planned values read-only.
- **From F2.2:** add a budget-lines check to `services.workspaces.BASE_CURRENCY_LOCKS` (any
  stored line locks the currency) and a source to
  `services.categories.USAGE_SOURCES` with `references` and `active_months`.
- **Closed-month guard (from F2.5).** Upsert, delete-on-zero and copy-from-previous call
  `services.months.ensure_month_open` for the **target** month inside their write transaction,
  after `require_role_now`. The auto-copy on first opening must skip closed months (and is a
  write during a page view: plan it explicitly).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.3 from docs/specs/M2-ledger-core.md: BudgetLine model + migration, service (upsert, delete-on-zero, copy-from-previous-month),
inline-editable planned cells in the month summary, banner + workspace setting for auto-copy.
Tests: unique per (month, category), copy never overwrites, closed-month protection.
```

---

## F2.4 Month summary (planned / actual / difference)

**Story:** At a glance I see how the month is going per category and overall.

**Acceptance**
- Pure domain function `summarize_month(transactions, budget_lines, categories) -> MonthSummary` (no I/O).
- Expense section: rows per category with planned, actual, difference (`planned - actual`), sorted by actual desc (toggle: manual order); totals row. Income section: planned, actual, difference (`actual - planned`).
- `treatment=savings` expense categories appear in a separate "Saved / invested" block; `transfer` is excluded; income categories with `treatment=savings` appear as "Withdrawn from savings".
- KPIs: `earned`, `spent`, `saved`, `net = earned - spent - saved`, `savings_rate = (earned - spent)/earned` (shown as "—" when earned = 0).
- Planned-vs-actual bars (ECharts or CSS bars) for total expenses and total income.
- Clicking a category filters the transaction tables to that category.
- Golden example (must be a test): budget Food 550.00; transactions Food 50.00 + 500.00 → actual 550.00, difference 0.00; budget Travel 5 350.00 with 4 323.00 spent → difference 1 027.00; income Salary planned 3 000.00, actual 3 400.00 → difference +400.00.
- Categories with actuals but no planned line are shown with planned 0.00 (never hidden).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.4 from docs/specs/M2-ledger-core.md: domain function summarize_month with the exact treatment/variance rules from
docs/specs/00-overview.md §3.3 and §3.5, the summary UI block (HTMX, refreshed via OOB after entry changes), KPI cards, and bars.
Property tests: totals equal the sum of rows; transfers never appear; difference sign convention. Include the golden example from the spec.
```

---

## F2.5 Month navigation and closing

**Acceptance**
- Prev/next month buttons, month picker, "Today" shortcut; URL-addressable months.
- Owner/editor can **close** a month (sets read-only for entries and planned values); owner can reopen. Closed state is shown clearly.
- Empty months (no data) still render; future months are allowed (for planning).

**As built (F2.5)**
- Pages: `/m` redirects (303) to the current month; `/m/YYYY-MM` is the month page (strict
  `YYYY-MM`, ASCII digits; months 1900-01..2199-12; anything else is a 404). The nav "Month" item
  links to `/m`; `/` stays the overview. The page header has Previous/Next (no link past the
  range ends), "Today", and a picker: a plain GET form with `<input type="month">` and a Go
  button, no JavaScript. Desktop Firefox and Safari render that input as a text box, so the
  server also accepts `2026/10`, `10.2026`, `10/2026`; an invalid value redirects back with an
  error. F2.2-F2.4 fill the `month_body` block of `templates/months/page.html`.
- "Today" is `ctx.clock.today(ctx.workspace.timezone)`: the workspace time zone setting (there is
  no per-user time zone). Month names come from Babel's CLDR data: `i18n.dates.format_month`
  keeps CLDR's case for running text ("listopad 2026"); `format_month_heading` capitalises the
  first letter for the page heading and title only.
- `domain.months.YearMonth`: parse/format, prev/next, first/last day, bounds.
- `MonthState` rows are created on the first close. Reopening clears `closed_at` and
  `closed_by_user_id` and keeps the row; rows are never deleted. Closing a closed month or
  reopening an open one changes nothing and records nothing; the page then says "already
  closed" / "already open".
- **Product decisions (owner, 2026-10-14):** a closed month is read-only for **everyone, owners
  included** (an owner reopens it explicitly, audited); only the **current and past** months can
  be closed (`MonthInFuture`, judged in the workspace time zone; the Close button is hidden on
  future months); `/` stays as it is; **every member, viewers included**, sees the closed banner
  with who closed it ("by a former member" when the account no longer exists).
- Roles: `close_month` needs editor, `reopen_month` owner; both check the Context first and
  `require_role_now` inside their `BEGIN IMMEDIATE` transaction.
- Guard: `services.months.ensure_month_open(session, ctx, month)` /
  `ensure_date_open(session, ctx, day)` raise `MonthClosed`; see F2.2 and F2.3 for the call
  sites (F2.2, F2.3, F2.6). Concurrency: on SQLite all writes are serialised by `BEGIN IMMEDIATE`.
  On PostgreSQL the guard takes `FOR SHARE` on the workspace row (`lock_month_writes`) and
  close/reopen take `FOR UPDATE` (`CurrentWorkspaceRepository.lock`), always before any other
  row, so a close waits for writes in flight and later writes see the month closed. A write that
  reads stored rows to find its months calls `lock_month_writes` before those reads.
- Audit: `month.closed`, `month.reopened` (data `month`: `YYYY-MM`; entity: the MonthState row),
  group "Closing months" in the workspace audit log.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.5 from docs/specs/M2-ledger-core.md: month navigation, MonthState model + migration, close/reopen with role rules,
read-only enforcement in the service layer (not only the UI). Tests for enforcement through the service API.
```

---

## F2.6 Transaction search and CSV export

**Acceptance**
- `/transactions` list across all months: filters (date range, direction, categories, text in description, amount range), sorting, pagination (50/page).
- Export the filtered result as CSV (UTF-8 with BOM option for Excel, delimiter and decimal-separator options); amounts as plain decimals.
- Editors can bulk-recategorize the filtered/selected rows (with confirmation and audit event).
- **Closed-month guard (from F2.5).** Bulk recategorize takes
  `services.months.lock_month_writes` before reading the selected rows, then refuses the **whole
  operation** if any selected row lies in a closed month, and the error lists those months. No
  row changes in that case. (F2.1's treatment change still applies to every month, closed ones
  included: it changes the category, not the transactions.)

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F2.6 from docs/specs/M2-ledger-core.md: cross-month transaction list with filters/sort/pagination, CSV export
(delimiter/decimal/BOM options), bulk recategorize with audit event. Use a query object in the repository, not ad-hoc SQL in routes.
Add an index review note to the PR (EXPLAIN for the main filters).
```
