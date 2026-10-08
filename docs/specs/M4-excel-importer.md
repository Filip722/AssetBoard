# M4 — Excel importer (dogfooding milestone)

**Goal:** bring your existing spreadsheet history into the app, safely and repeatably, so you can stop using the spreadsheet for monthly tracking.

**Exit criteria:** you can upload your workbook, review a dry-run report, map categories, import all month sheets, and the imported per-category actual totals match the totals the workbook itself shows.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Privacy rule:** the real workbook is never committed. The repo contains `scripts/make_sample_workbook.py` that generates a synthetic workbook with the same layout (see F4.1).

**Data model (sketch):** `ImportBatch(id, workspace_id, kind, filename, file_sha256, started_by, started_at, finished_at?, status[dry_run|imported|rolled_back], report JSON)`; `Transaction.import_batch_id`; for dedupe, `Transaction.import_key` (hash of sheet name + row index + normalized values), unique per workspace when not null.

**Layout of the source workbook (as observed; the importer must be tolerant)**
- Month sheets are named like `08.26`, `01.2025`, `1.2024` (month `M`/`MM`, then 2- or 4-digit year); many are **hidden** (import hidden sheets too); sheet order is not chronological.
- Expenses block: columns B–E = day of month, amount, description, category (header row 3, data from row 4). Income block: columns G–J = day, amount, description, category.
- Summary block: expense categories with planned/actual/difference in columns M, O, P, Q (planned in O); income categories with planned/actual/difference in S, U, V, W (planned in U, actual in V). Some cells contain formulas or `#REF!`; read **cached values**.
- The income summary contains a special row labelled like "days worked previous month" with a number in the planned column — this is not a category; treat it as a **work-log hint**.

---

## F4.1 Import framework and sample workbook

**Story:** Imports are dry-run first, idempotent, and reversible.

**Acceptance**
- Parser interface: `parse(file) -> ParsedData` (pure, no DB) and `plan(ParsedData, workspace) -> ImportPlan` (what would be created/skipped/flagged), `apply(plan)`.
- Upload page (editor/owner): file upload (size limit configurable, `.xlsx` only), runs parser in a worker thread with timeout; shows the **dry-run report** (counts, per-sheet table, warnings, errors) before anything is written.
- `ImportBatch` stored with the report; **rollback batch** soft-deletes everything created by it.
- Re-importing the same file creates nothing new (dedupe via `import_key`); changed rows are reported as "conflicts" and skipped by default.
- Uses `openpyxl` with `data_only=True` (cached values) in read-only mode; never evaluates formulas, never executes macros.
- `scripts/make_sample_workbook.py` generates a synthetic workbook (≥ 12 month sheets, hidden sheets, mixed sheet-name formats, a `#REF!` cell, an out-of-order sheet, a special "days worked" row) used by tests.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.1 from docs/specs/M4-excel-importer.md: ImportBatch model + migration, parser/plan/apply interfaces, upload + dry-run report UI,
rollback, idempotent re-import via import_key, openpyxl read-only data_only parsing with size/time limits,
and scripts/make_sample_workbook.py generating the synthetic workbook described in the spec.
No month-sheet parsing logic yet beyond discovering sheet names (that is F4.2). Tests with the synthetic workbook only.
```

---

## F4.2 Month sheets importer

**Story:** All my month sheets become transactions and planned values.

**Acceptance**
- Sheet-name parser: `^(\d{1,2})\.(\d{2}|\d{4})$` → month/year (2-digit year → 2000+). Non-matching sheets are ignored and listed.
- Each expense/income row with a numeric amount becomes a `Transaction`; date = (sheet year, sheet month, day). Day out of range for the month → flagged row, imported on the last day of the month with a warning (configurable: skip).
- Rows with empty amount are skipped silently; non-numeric/`#REF!`/negative amounts are reported as errors and skipped.
- Planned values from the summary blocks become `BudgetLine`s.
- Category names are taken from the entry rows and summary blocks (trimmed, case-insensitive match).
- Summary-only rows that are not categories (the "days worked" row) are collected as hints and not imported as transactions or categories.
- Month import is atomic per sheet.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.2 from docs/specs/M4-excel-importer.md: month-sheet discovery by name pattern, parsing of the expense/income blocks and
the planned columns of the summary blocks, row-level warnings/errors as listed, creation of Transactions and BudgetLines, per-sheet atomicity.
Extend the synthetic workbook generator if needed. Tests: name patterns, out-of-range days, bad amounts, hidden sheets, idempotency.
```

---

## F4.3 Category mapping

**Story:** My spreadsheet categories map onto app categories (including which ones are savings), and I confirm that before importing.

**Acceptance**
- Mapping step between dry-run and apply: table of source category names → existing category / create new / ignore, per direction.
- Smart defaults: exact (case-insensitive) matches are pre-selected; names that match known savings words (investments, savings, and their Polish equivalents) are proposed as `treatment=savings`; the user can change it.
- The mapping can be saved and reused for the next import.
- Unmapped categories block `apply` until resolved.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.3 from docs/specs/M4-excel-importer.md: mapping UI and service between dry-run and apply, defaults with savings-word heuristics
(keep the word list in a data file, en + pl), saved mappings reused on later imports, apply blocked until all categories resolved.
```

---

## F4.4 Validation and reconciliation report

**Story:** I can trust the import because the totals match the workbook.

**Acceptance**
- For each month sheet and category, compare **imported actual sums** to the **actual values cached in the sheet's summary block** (expense column P, income column V in the observed layout). Differences are shown in a reconciliation table with status ✔ / ✖ and the delta.
- Sheet-level checks: duplicate rows (same day, amount, description), future-dated rows, months with entries but no summary.
- Report downloadable as CSV.
- A "needs attention" count appears on the import batch and blocks the "I've reviewed this" confirmation until acknowledged.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.4 from docs/specs/M4-excel-importer.md: reconciliation of imported sums vs the sheet's cached summary actuals per month and category,
sheet-level checks (duplicates, future dates, missing summary), CSV download, acknowledgement gate on the batch.
Tests on the synthetic workbook including a deliberately inconsistent sheet.
```

---

## F4.5 Work-log hints

**Acceptance:** if the special "days worked previous month" values exist and a variable income source is configured (M3), offer to create `WorkLog` entries (work month = sheet month − 1). Never overwrites existing logs. Skipped silently if no variable income source exists.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.5 from docs/specs/M4-excel-importer.md: turn collected work-log hints into WorkLog proposals the user can accept per month,
never overwriting existing logs. Tests with and without a variable income source configured.
```

---

## F4.6 (stretch) Generic CSV import

**Story:** I can import bank statements.

**Acceptance:** upload CSV; delimiter/encoding/decimal-separator detection with manual override; column mapping (date, amount, description, optional category, optional debit/credit columns); date format selection; preview of the first 20 rows; dedupe by hash of (date, amount, description); category suggestion from previous descriptions; saved mapping profiles ("mBank", "Revolut", user-named). Uses the same dry-run → apply → rollback framework.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.6 (stretch) from docs/specs/M4-excel-importer.md on top of the F4.1 framework: CSV upload, detection with overrides,
mapping profiles, preview, dedupe, category suggestions. Provide synthetic sample CSVs for tests (no real bank data).
```
