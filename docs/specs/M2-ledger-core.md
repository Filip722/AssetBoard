# M2 — Ledger core

**Goal:** replace the monthly spreadsheet tabs: categories, income and expense entries, planned values per category, and the planned / actual / difference summary.

**Exit criteria:** for any month you can enter expenses and income quickly, set planned values per category, and see the same summary your spreadsheet shows (plus savings rate). Data is workspace-scoped and role-protected.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `Category(id, workspace_id, name, name_key, kind[expense|income], treatment[regular|savings|transfer], color, sort_order, archived_at)`; unique `(workspace_id, kind, name_key)` among non-archived (partial unique index `WHERE archived_at IS NULL`, on SQLite and PostgreSQL). `name_key` is computed in Python (`domain.categories.name_key`: NFKC, Unicode casefold, whitespace collapsed) because SQLite's `lower()` folds ASCII only. `UNIQUE(workspace_id, id)` is the target of composite foreign keys from Transaction and BudgetLine. (Changed in F2.1; was `lower(name)`.)
- `Transaction(id, workspace_id, direction[expense|income], date, amount_minor, currency, category_id, description, description_key, source, created_by_user_id?, created_at, updated_at, deleted_at)`; index `(workspace_id, date)`, `(workspace_id, category_id, date)`, `(workspace_id, direction, description_key)`. (Changed in F2.2: `created_by` is `created_by_user_id`, set to NULL when the account is deleted; `description_key` is the casefolded description for autocomplete; the reserved `scheduled_occurrence_id`, `import_batch_id`, `account_id` columns are added with their composite foreign keys by M3, M4 and the accounts feature, whose tables do not exist yet.)
- `BudgetLine(id, workspace_id, month, category_id, planned_minor, currency, created_at, updated_at)`; unique `(workspace_id, month, category_id)`; composite FK to `categories(workspace_id, id)`. (Changed in F2.3: timestamps; zero removes the row, no `deleted_at`.)
- `MonthState(id, workspace_id, month, closed_at?, closed_by_user_id?, plan_started_at?)`; `month` is the first day (DATE); unique `(workspace_id, month)`. (Changed in F2.5: `id` key for the scoped repository; `closed_by_user_id` is set to NULL when the account is deleted. F2.3 adds `plan_started_at`, the automatic-copy marker.)

**Carried over from F1.2 (2026-10-12):** owners can change a workspace's base currency on the
settings page, which is harmless while no amounts exist. Once transactions or budget lines
exist, M2 must either block the change or convert stored amounts; decide in F2.2's plan.
**Decided in F2.2 (owner, 2026-10-14):** block it. The base currency cannot change once any
transaction or budget line exists, soft-deleted rows included; the settings page shows it locked
with the reason (`services.workspaces.BASE_CURRENCY_LOCKS`; F2.2 adds transactions, F2.3 budget
lines). **Changed in F2.3:** budget lines are removed, not soft-deleted, so only lines that exist
now lock it (owner decision).

---

## F2.1 Categories

**Story:** I manage my expense and income categories, including which ones are really savings or transfers.

**Acceptance**
- CRUD for categories: name, kind, treatment, color, order (drag or up/down buttons). Archive instead of delete when the category has transactions.
- Every new workspace gets a seed set (en + pl names) (changed in pre-release A; was: "First run offers a seed set"): expenses — Food, Eating out, Gifts, Health, Housing, Transport, Cosmetics, Hobbies, Utilities, Travel, Entertainment, Education, Taxes, Clothes, Other; savings (expense, treatment `savings`) — Savings, Investments; income — Salary, Bonus, Interest, Sale of items, Other; savings withdrawal (income, treatment `savings`).
- Changing `kind` of a category that has transactions is blocked; changing `treatment` is allowed and shows how many past transactions are affected.
- Editors/owners only; viewers read-only.

**As built (F2.1)**
- "In use" comes from `services.categories.USAGE_SOURCES`, a static list (F2.2 changed it from import-time registration; `CategoryUsage` lives in `services.category_usage`). F2.2 adds transactions: `references` counts every transaction row of the category, soft-deleted ones included; `active_transactions` counts non-deleted ones (any date; shown as "affected" on a treatment change); `active_months` are the months with non-deleted rows. **F2.3** (budget lines) adds `references` and `active_months` (planned values change closed months' totals too); **M3** (scheduled items) `references` only.
- **Treatment change and closed months (owner, F2.2):** allowed, but the edit row warns with the closed months among `active_months` ("This changes the totals of 2 closed months: 2026-08, 2026-09.", at most 12 listed); `category.updated` records `closed_months_affected` (a count).
- Deleting a category in use archives it ("Archived because it is in use"); the Delete button is shown only for unused categories, Archive/Restore always. Restoring fails while an active category of the same kind has the name; a restored category goes to the end of its list.
- **Automatic seed (owner, pre-release A, 2026-10-18; replaces "nothing is seeded automatically"):** every new workspace gets the seed set in the same transaction that creates it, with no question asked: first-run setup, open registration, `assetboard create-admin` (unless `--no-workspace`), and OIDC or proxy auto-create (an invitation sign-up creates no workspace). Names are in the creating request's language (the user's saved language, else the browser's); `create-admin` uses `--locale`, else `DEFAULT_LOCALE`. Recorded as `category.seeded` in the new workspace with `automatic: true`, by the same actor as the creation (the new user; `system` for setup; `cli` for `create-admin`). Self-service workspace creation (pre-release A part 3) seeds the same way.
- "Add suggested categories" stays only as a fallback for a workspace with no categories at all (archived included), such as one created before the automatic seed. Seed names are stored in the language of the user who clicks, and are ordinary names afterwards.
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

**As built (F2.2 part B: the month-page UI)**
- Templates: `months/_ledger.html` (macros; the input, button and error classes are set once
  there), `_section.html`, `_row.html`, `_suggestions.html`, `_summary.html`; view models and
  messages in `web.ledger`, routes in `web.transaction_routes`. The tables are real `<table>`s:
  a form cannot wrap a row, so each entry/edit `<form>` sits in the actions cell and its inputs
  join it with the `form` attribute. The entry row is at the bottom of each table. Day and
  amount are text inputs (`inputmode` numeric/decimal); the amount of an edit row is written by
  `i18n.numbers.format_amount_input` (locale decimal sign, no grouping) and read back by
  `parse_amount`.
- Routes: `POST /m/{month}/transactions` (create, with `direction`), `GET
  /m/{month}/transactions/{id}/edit` and `/row` (open/cancel an edit; one-row swaps),
  `POST /m/{month}/transactions/{id}` (update), `/delete`, `/undo`, and `GET
  /transactions/suggestions?direction=&description=` (`role="option"` items). **Deviation from
  "row swaps":** writes return the whole table section (`#ledger-expense` / `#ledger-income`),
  since rows are ordered by date and an edit can move a row or send it to another month; only
  opening and cancelling an edit swap one row. Every write response also carries, out of band,
  `#month-summary` (F2.3 fills it with the plan, `web.plan.summary_context`; **F2.4 adds the
  actual and difference columns** to `months/_summary.html`) and the screen-reader status line `#ledger-status`.
- Without JavaScript every form posts and redirects (303); a refused save re-renders the whole
  page (400/409) with the values and messages; `?edit=<id>` opens a row for editing,
  `?deleted=<id>` shows "Entry deleted." with an Undo form, `?saved_in=YYYY-MM` links an entry
  saved to another month. Autocomplete needs JavaScript (no fallback).
- `MonthClosed`: when the **viewed** month was closed meanwhile the page is stale, so the route
  answers 303 (HTMX: 409 with `HX-Redirect`) to `?error=month_closed`, which shows
  `MONTH_CLOSED_MESSAGE`; a full date in **another**, closed month is a message on the day field
  (409) and keeps the typed values. A row that no longer exists reloads with "This entry no
  longer exists" (`?error=transaction_gone`).
- Keyboard and focus (`static/js/app.js`, delegated listeners on `data-*` hooks, no Alpine
  components): Enter submits from text fields (implicit submission) and from the category
  select; Esc closes an open suggestion list, else clears the entry row or cancels an edit. The
  server marks the element to focus after a swap with `data-autofocus` (new entry row's day after
  a save or delete, the first invalid field after a refusal, the edit row's day, the row's Edit
  button after a save or cancel). The description input is an ARIA combobox; arrows move the
  active option, Enter or a click picks it and pre-selects its category.
- **Product decisions (owner, 2026-10-18):** after a save the next entry row **keeps the day**
  of the entry just saved (per table): as a day number when it is in the viewed month, else as
  typed; the focus is in the day field with its text selected, so typing replaces it and Tab
  keeps it. Without JavaScript the 303 carries it as `?entry=<direction>&day=<day>` (validated
  against the day forms `parse_entry_date` reads; anything else is ignored). No delete
  confirmation (undo replaces it); no totals in part B (F2.4); the undo toast is **strict**
  (`data-toast-strict`: no pause on hover or focus, since the server window does not pause) and
  counts down the time the server still allows (from `undo_until`); after a delete the focus
  goes to the table's entry day field (its heading when the table has no entry row). A click
  after the window says "Too late to undo". The no-JS Undo form is shown only for a row of this
  month whose deletion can still be undone (`services.transactions.pending_undo`).
- **Typing on during a save (review, 2026-10-18):** on `htmx:beforeRequest` from an entry form
  (its values are already serialised) `app.js` resets the entry row at once (the day kept by the
  rule above) and focuses the day field; before a 2xx swap of a table section (also an
  out-of-band one, as after an undo) it keeps the entry row's values, focused field and
  selection, and puts them into the new row after the swap instead of the server's
  `data-autofocus`. On a 4xx the server's echo of the submitted values and messages wins; if
  sending fails, the submitted values come back.
- **Swaps settle at once (fix, 2026-10-18):** `base.html` sets HTMX's `defaultSettleDelay` to 0.
  With the default 20 ms, new content was visible but not yet wired up (`hx-*`) and its
  `data-autofocus` not yet applied: Esc right after opening an edit row clicked an inert Cancel
  link, and a field clicked in that window lost the focus to the day field, so typing meant for
  the amount went into the day (the intermittent F2.2 browser failure). No style depends on the
  settling classes.

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

**As built (F2.3 part A: data, service, seams and the workspace setting; the month-page UI is
part B)**
- `BudgetLine` as sketched, plus `created_at`/`updated_at`; composite foreign key
  `(workspace_id, category_id) -> categories(workspace_id, id)`; `UNIQUE(workspace_id, month,
  category_id)`; checks `planned_minor` 1..999 999 999 999 999, `month` 1900-01-01..2199-12-01;
  index `(workspace_id, category_id)`. `month` is the first day (the repository takes a
  `YearMonth`). No `deleted_at`: **zero or empty removes the row** (hard delete; no undo for
  planned values).
- Currency: the workspace's base currency, read from the workspace row inside the write.
  Amounts parse in the caller's locale (`i18n.numbers.parse_amount`) and convert with
  `domain.budget.planned_minor`, which refuses instead of rounding: `invalid_amount`,
  `amount_negative`, `amount_too_large`, `too_many_decimals`; `0` (also `0,00`, `-0`) and an
  empty field mean "remove". Syntax and sign are checked before the write; the currency-specific
  checks (decimals, size) run inside it with the currency read from the workspace row, never the
  Context's (review, 2026-10-18).
- Service `services.budget`: `get_plan` (every member), `set_planned` (outcome `created`,
  `updated`, `removed` or `unchanged`; saving the stored value or removing a missing line
  changes and records nothing), `copy_previous_plan`, `auto_copy_on_open`; seams
  `category_usage` and `workspace_has_budget_lines`.
- Writes: `require_role` on the Context, then in one `run_immediate` transaction
  `require_role_now`, the **exclusive** workspace lock (orders plan writes against each other on
  PostgreSQL, so concurrent upserts or copies cannot both insert one (month, category); it also
  covers `lock_month_writes`), `ensure_month_open` for the target month, and only then reads.
- A **new** line needs an active category that is not a transfer (`category_archived`,
  `category_transfer`); an existing line can be changed or removed whatever its category became.
  An unknown or foreign category is `NotFound`.
- `get_plan` groups rows like F2.4's summary: `expense` (expense/regular), `saved`
  (expense/savings), `income`, `withdrawn` (income/savings), and `transfer` ("Transfers (not
  counted)") only when such lines exist; rows are the active non-transfer categories in category
  order plus archived or transfer categories that have a line in the month.
  `copyable_from_previous` counts the lines a copy would add now (for the banner).
- **Copy:** strictly from the immediately previous month; never overwrites (categories that
  already have a line keep it: `kept`); lines of categories now archived or transfers are
  skipped and counted (`skipped`). A closed **source** month can be copied from.
- **Automatic copy:** workspace setting `workspaces.auto_copy_plan` (default off; owners, on the
  settings page). `auto_copy_on_open` copies when the actor is an editor or owner, the month is
  the **current one or later** (past months get the banner only), open, has no line and **never
  had a planned value**: `month_states.plan_started_at` is set by the month's first plan write
  (save, copy or automatic copy) and never cleared, so a plan emptied on purpose is not
  refilled. A read decides first, so a page view writes only when a copy is due; the write
  checks again after the exclusive lock and the month guard, and swallows `PermissionDenied`,
  `MonthClosed` and SQLite's "database is locked" (a page view never fails because of it). Nothing is written when the previous month has nothing to copy, so a later visit can
  still copy. It is a GET that writes by design: idempotent (at most once per month, by the
  marker), race-safe (serialised write, re-checked), and without CSRF risk beyond what the
  owner's setting already does on a visit. Part B calls it from the month page GET (not HEAD).
- **Base-currency lock (deviation from "soft-deleted rows included"):** lines are removed, not
  soft-deleted, so `workspace_has_budget_lines` is true while a line exists now. A removed plan
  holds no amount; audit rows carry `currency`. With no transaction and no line left, the
  currency can change again.
- Usage source: `references` counts lines (any month), `active_months` their months. A planned
  value therefore blocks a kind change and turns a delete into archive, and a treatment change
  lists closed months that have planned values.
- Audit: `budget_line.created` (`month`, `category`, `amount`, `currency`), `.updated` (adds
  `previous_amount`), `.deleted` (the removed amount), `budget_line.copied` (entity: the month's
  `MonthState` row; `month`, `source_month`, `count`, `skipped`, `automatic`), group "Planned
  values". Every cell save that changes something is audited.
- **Product decisions (owner, 2026-10-18):** automatic copy only for the current and later
  months (banner for past months); copy skips archived and transfer categories and reports the
  count; no new planned values on transfer categories (existing ones shown under "Transfers
  (not counted)" so they can be removed); removing every line with no transaction unlocks the
  base currency; every cell save is audited; copy strictly from the immediately previous month.

**As built (F2.3 part B: the month-page UI)**
- **Deviation from "the summary's planned cells":** F2.4 builds the summary after F2.3, so F2.3
  builds the summary block's frame: `#month-summary` (`months/_summary.html`, heading "Plan")
  with F2.4's groups as `<tbody>`s and two columns, Category and Planned. **F2.4 adds Actual and
  Difference to these rows**, plus totals, KPIs, bars and sorting. View models and messages are
  in `web.plan` (`build_summary`, `summary_context`; this replaces F2.2's placeholder
  `web.ledger.summary_context`), routes in `web.plan_routes`, macros in `months/_plan.html`.
- Each planned cell is a one-input `<form>` inside its `<td>` (`hx-trigger="change, submit"`,
  `hx-target` its row): without JavaScript Enter submits it (implicit submission) and the page
  redirects (303, `?notice=plan_saved|plan_removed#plan-<category>`); a refused value re-renders
  the page (400) with what was typed and the message, focused. With HTMX the response is that
  row (400 for a refused value, without moving the focus: the user may be typing in the next
  cell) with the copy banner `#plan-banner` and the screen-reader line `#plan-status` out of
  band. Viewers and closed months see the amounts as text ("—" when not planned).
- Routes: `POST /m/{month}/plan/{category_id}` (`plan_set`, field `amount`), `POST
  /m/{month}/plan/copy` (`plan_copy`; HTMX: the whole `#month-summary`, focus on the first cell;
  without JavaScript 303 with `?plan_copied=N&plan_skipped=M`). A month closed meanwhile: 303 or
  `HX-Redirect` to `?error=month_closed`; an unknown category: `?error=plan_category_gone` (404).
- Banner: "This month has no plan yet." with "Copy plan from <month>" when the viewer can write,
  the month is open, has no line and the previous month has lines a copy would add. Month names
  after "from" use `i18n.dates.format_month_after_from` (CLDR's format-context name: "z
  września 2026"). After a copy: "Copied N planned values from …" (or "… (workspace setting)"
  for the automatic copy), and how many were not copied.
- The month page GET calls `services.budget.auto_copy_on_open` before rendering (HEAD answers
  405: FastAPI's GET routes do not take HEAD). `?plan_copied=`/`plan_skipped=` are read only as
  1-6 ASCII digits (anything else is ignored), and the copy result is shown to writers only.
- A new value for a category that left the plan meanwhile (archived or turned into a transfer
  in another tab, no line) reloads the page with the message (`?error=plan_category_archived` /
  `plan_category_transfer`; HTMX: 409 with `HX-Redirect`), since its row is gone. The
  too-many-decimals message names the stored currency the value was judged in.
- The screen-reader line `#plan-status` sits outside `#month-summary` (a transaction write's
  out-of-band summary never clears it); a copy announces its result there. A cell's save that
  completes after a transaction write replaced the block swaps into the new block's row of the
  same id (`app.js` listens on the row itself: HTMX fires `htmx:beforeSwap` on the detached
  row). Empty read-only cells show "—" with "Not planned" as screen-reader text.
- Every transaction write renders the whole block out of band. `app.js` (delegated listeners):
  Enter in a planned cell moves to the next cell (the change saves the one left; on the last
  cell Enter saves in place), Esc restores the saved value (`data-saved-value`) and clears the
  message; a planned value being typed when a transaction write swaps the block keeps its text,
  focus and selection and is saved once when the field is left; the server's `data-autofocus`
  of the transaction table does not pull the focus out of a planned cell.

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
- **From F2.2/F2.3:** render the summary in `months/_summary.html` from
  `web.plan.summary_context` (F2.3 built the rows with planned cells, `months/_plan.html`);
  every transaction write already sends `#month-summary` out of band, so the totals refresh
  without a reload.

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
- `MonthState` rows are created on the first close (and, since F2.3, on a month's first
  planned-value write, which sets `plan_started_at`). Reopening clears `closed_at` and
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
