# M4 — Ledger export and import (full workspace backup)

**Goal:** your ledger is never locked in. One documented, versioned archive format (a ZIP of
CSV files plus `manifest.json`) is used for both export and import: it is a full backup of a
workspace's ledger that restores with full fidelity, and its CSV files are simple enough that
you can convert your own spreadsheet history into them (by hand, with a script, or with an AI
assistant) and import that.

**Exit criteria:** you can export a workspace, restore the archive into an empty workspace and
get the same categories, transactions (deleted ones included), planned values, closed months
and settings back; you can import a hand-made `transactions.csv` (and `budget_lines.csv`) into
a workspace that is already in use, after a dry-run report with per-month and per-category
totals that you compare with your spreadsheet; every import can be rolled back, and repeating
an import creates nothing new.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Out of scope (owner decisions, 2026-10-18)**
- No importer for a particular spreadsheet layout (no `openpyxl`, no sheet-name parsing, no
  synthetic workbook, no reconciliation against a workbook's cached cells, no work-log hints).
  Everyone's spreadsheet differs; users convert their data into the format below themselves.
  The repository contains no conversion script for anyone's workbook.
- No generic column-mapping importer for arbitrary CSV files and no bank-export conventions
  (negative amount = expense, no direction column, debit/credit columns): post-1.0, see
  [backlog-post-1.0.md](backlog-post-1.0.md#generic-csv-import-with-column-mapping).
- The app never calls an AI service or any other external service for import or export (§1 of
  the overview, AGENTS.md). The documented AI prompt (F4.5) is something the user may choose to
  use outside the app.

**Format decision:** [ADR-0016](../adr/0016-workspace-archive-format.md) records the archive
format and its versioning and compatibility policy. The [format reference](#format-reference-v10)
at the end of this spec is normative for F4.1–F4.4; F4.5 turns it into user documentation.

**Prerequisite (backlog, from the F1.1 review):** before any upload route exists, add a pure-ASGI
middleware that caps the request body size (a small default for forms, a larger, explicit limit
only for the upload route), rejects oversized bodies with 413 without buffering them, and works
without a reverse proxy. It is part of F4.2. Until then the deployment guide asks operators to
cap body size at the proxy.

**Privacy rule:** no real personal data in the repository. Test archives and CSV files are
synthetic and built by test builders (committed golden archives under `tests/fixtures/` are
synthetic too).

**Data model (sketch)**
- `ImportBatch(id, workspace_id, source[archive|transactions_csv|budget_lines_csv|categories_csv],
  mode[restore|merge], filename, file_sha256, size_bytes, options JSON (delimiter, decimal
  separator), status[uploaded|planned|applied|rolled_back|cancelled|failed],
  started_by_user_id?, created_at, updated_at, applied_at?, rolled_back_at?, report JSON,
  previous_settings JSON?)`. `filename` is the uploaded name, sanitised and cut to 255
  characters; `previous_settings` holds the workspace settings a restore replaced (for its
  rollback). `UNIQUE(workspace_id, id)` is the target of composite foreign keys.
- `Transaction.import_batch_id?`, `Transaction.import_key?`; `Category.import_batch_id?`,
  `Category.import_key?`; `BudgetLine.import_batch_id?` (composite foreign keys to
  `import_batches(workspace_id, id)`). `import_key` is unique per workspace when not null
  (partial unique index on SQLite and PostgreSQL) and is either `id:<uuid>` (the row came from a
  file row with an `id`: its lineage) or `c1:<sha256 hex>` (content key, see F4.2).
- `ImportCategoryMapping(id, workspace_id, kind, source_name_key, category_id?, action[map|ignore],
  updated_at)`; unique `(workspace_id, kind, source_name_key)`; composite foreign key to
  `categories(workspace_id, id)` with `ON DELETE CASCADE`. Saved mappings (F4.3).
- The uploaded file is kept, between upload and apply only, under
  `DATA_DIR/imports/<workspace_id>/<batch_id>` (mode 0600, outside any served directory), and
  deleted on apply, cancel or failure, or by an hourly job 24 hours after upload.

**Roles (decided here; see open question 1 for viewers)**

| Action | Viewer | Editor | Owner |
|---|---|---|---|
| Export the archive (F4.1) | no (open question) | yes | yes |
| Import a lone CSV or merge an archive (F4.3, F4.4) | no | yes | yes |
| Roll back a merge batch | no | yes | yes |
| Restore an archive into an empty workspace, incl. settings and closed months (F4.4) | no | no | yes |
| Roll back a restore | no | no | yes |

Roles are checked on the Context and again with `require_role_now` inside every write
(docs/architecture.md).

**Closed months (owner decision, 2026-10-18, closes the F2.5 open question):** rows (transactions
or planned values) dated in a month that is closed in the target workspace are **skipped and
listed** in the dry-run report; they never refuse the whole import. A restore into an empty
workspace recreates closed months itself, **after** all data is written (order in F4.4).
Rolling back a batch that has rows in a month closed since then is refused as a whole, listing
those months (the same rule as F2.6's bulk recategorize).

---

## F4.1 Archive format and full export

**Story:** I download my whole workspace ledger as one documented file I can keep, read and
restore.

**Acceptance**
- Pure module `domain.archive` (in-memory bytes only, no DB, no clock): row types for every file
  of the [format reference](#format-reference-v10), a writer (`rows -> CSV bytes`, `files ->
  manifest`) and a reader/validator (`bytes -> ParsedArchive | errors`) shared by F4.2–F4.4.
  Standard library only (`csv`, `zipfile`, `json`, `hashlib`, `io`); no new runtime dependency.
- Export service `services.archive.export_workspace(ctx)`: editors and owners; reads through the
  scoped repositories only; writes the files of format 1.0 (`manifest.json`, `workspace.json`,
  `categories.csv`, `budget_lines.csv`, `transactions.csv`, `months.csv`) exactly as specified,
  including soft-deleted transactions (with `deleted_at`) and archived categories.
- **Deterministic output:** the same data and the same `exported_at` give byte-identical ZIP
  bytes (fixed file order, fixed row order, ZIP entry timestamps = `exported_at`, deflate, no
  extra fields). Tested against a committed golden archive built from a synthetic workspace.
- **Lineage ids:** the `id` column of `transactions.csv` and `categories.csv` is the row's origin
  id: the UUID in its `import_key` when that is `id:<uuid>`, else the row's own id. So export →
  restore elsewhere → export gives the same ids, and an archive merged back into its source
  workspace recognises every row (F4.2 dedupe).
- Manifest: `format`, `format_version`, `app` (name from `branding.APP_NAME`, version),
  `exported_at`, `base_currency`, `files` (path, SHA-256, rows, bytes). The reader verifies every
  checksum and row count and that `base_currency` equals `workspace.json`'s.
- Export page "Export and import" (`/workspace/data`, linked from the workspace menu for editors
  and owners): a "Download archive" button (POST with CSRF; the response is the ZIP, streamed
  from a temporary file, `Content-Disposition` file name
  `<branding.CLI_NAME>-<workspace-slug>-<YYYYMMDD-HHMMSS>Z.zip`, `Cache-Control: no-store`).
- Audit `workspace.exported` (counts per file, never amounts or text); group "Import and
  export". Nothing about the contents is logged.
- **Archive coverage test:** every workspace-owned model is either written to the archive or
  listed in `ARCHIVE_EXCLUDED_MODELS` with a reason (memberships, invitations, audit events,
  import batches, saved mappings, ...). Adding a workspace-owned model without deciding fails the
  test; this is how M3 and M5–M8 cannot forget their archive extension.
- **Cross-reference F2.6:** F2.6's filtered CSV export writes the `transactions.csv` columns in
  the same order (without `deleted_at`); with its default options it is a valid
  `transactions.csv`, and with the semicolon/decimal-comma options it imports with the matching
  upload options (F4.3).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.1 from docs/specs/M4-ledger-export-import.md: the pure domain.archive module (row types, CSV/manifest writer,
reader/validator per the "Format reference v1.0" section and ADR-0016), the export service and the /workspace/data page
with the download button, lineage ids, deterministic ZIP output, the workspace.exported audit event and the archive
coverage test (ARCHIVE_EXCLUDED_MODELS). Standard library only. Tests: golden archive from a synthetic workspace,
byte-for-byte determinism, checksum/row-count verification, soft-deleted and archived rows exported, viewer refused.
```

---

## F4.2 Import framework: upload, limits, dry-run, batches, rollback, dedupe

**Story:** Every import is checked before anything is written, can be repeated safely, and can
be undone.

**Acceptance**
- **Body-size middleware** (prerequisite above): pure ASGI, counts bytes as they arrive, answers
  413 without buffering; the limit for every route is `ASSETBOARD_MAX_REQUEST_BYTES` (default
  1 MiB), for the upload route only `ASSETBOARD_IMPORT_MAX_UPLOAD_BYTES` (default 20 MiB). Both
  are documented in the deployment guide's environment table, and its proxy advice is updated
  (the proxy must allow the upload path's larger size).
- Upload (editors/owners; CSRF; multipart): accepts a `.zip` archive or a lone `.csv` (F4.3 says
  which). Content decides, not the extension: a ZIP must start with the local-file signature; a
  CSV must decode as UTF-8 (a leading BOM is removed; invalid UTF-8 is an error naming the line).
  The file is stored as described in the data model; an `ImportBatch` (`uploaded`) records its
  SHA-256 and size.
- **ZIP safety:** the archive is never extracted to disk; members are read in memory through a
  counting reader that stops at the limit (header sizes are not trusted). Refused, with a clear
  error: members in folders or with `/`, `\`, `..`, absolute or drive-letter names; symlinks or
  other non-regular entries; encrypted members; methods other than stored and deflate; duplicate
  names; more than 64 members; a member over 100 MiB or a total over 200 MiB uncompressed; a
  member with a compression ratio above 200:1 (members under 1 MiB exempt). Unknown member names
  are listed as warnings and ignored.
- **Parse and plan in a worker thread with a time limit** (60 s, a code constant): parsing is
  pure (`domain.archive`), planning reads the workspace through services and writes nothing.
  Over the limit the batch is `failed` with a message. At most 1 000 000 data rows per upload.
- **Dry-run report** shown before anything is written, stored in `ImportBatch.report`, status
  `planned`: counts (to create, duplicates skipped, conflicts skipped, skipped in closed months,
  skipped for ignored categories, invalid); a **per-month table** (month; expense total, income
  total, planned total; rows to create; rows skipped) and **per-category totals** per kind of
  what will be imported, so the user compares them with their own spreadsheet (this replaces
  the old workbook reconciliation); the closed months with their skipped rows; warnings (future
  dates, unknown columns, unknown files, settings that differ in merge mode); errors with file,
  line and column. Errors and skipped rows are downloadable as CSV
  (`file,line,column,code,message`).
- **Invalid rows:** in merge mode the user can apply the valid rows only after ticking "Skip N
  invalid rows"; in restore mode any error blocks the apply (a restore must be complete).
- **Apply** re-parses the stored file and re-plans in the write transaction (the workspace may
  have changed since the dry-run); if the counts differ from the confirmed report, nothing is
  written and the new report is shown for confirmation again. Apply runs in one transaction
  (`run_immediate`, `require_role_now`, then the exclusive workspace lock, which also covers
  `lock_month_writes`), so a batch is all or nothing. Imported transactions get
  `source=import`, `import_batch_id` and `import_key`; amounts use the currency read from the
  workspace row inside the transaction.
- **Dedupe (idempotent re-import):**
  - A row with an `id`: it is a duplicate when the workspace has a transaction (or category)
    with that id or with `import_key = id:<id>`; if its values differ, it is a **conflict**,
    skipped and listed ("changed since the export").
  - A row without an `id`: content key `c1:` + SHA-256 of `date`, `direction`, amount in minor
    units, currency, source category name key (as in the file, before mapping), description key
    (F2.2's `description_key`) and the **occurrence number** of that same tuple within the file
    (1, 2, ...), joined with U+001F. Importing the same file twice creates nothing the second
    time, while two genuinely identical rows in one file (two coffees, same day, same price) are
    both imported; a later file that repeats a month with one more identical row adds only that
    row.
  - Soft-deleted rows keep their keys: a row deleted after an import is not brought back by
    re-importing; it is reported as "imported before and deleted".
  - Budget lines dedupe on `(month, category)`: same amount = duplicate, different amount =
    conflict (the stored value is kept, listed). Categories: see F4.3.
- **Rollback** of an applied merge batch (editors/owners; confirm page with counts): soft-deletes
  its transactions (edited ones too; the confirm page says how many were edited), removes its
  budget lines (hard delete, as F2.3 does) except lines changed after the import
  (`updated_at > applied_at`, kept and listed), and removes the categories it created through
  F2.1's rule (delete when unused, else archive). Refused as a whole when any affected month is
  closed now (the months are listed). Restore rollback: F4.4.
- **Cancel** of a planned batch deletes the stored file (`cancelled`). `/workspace/data` lists
  recent batches with status, counts and actions.
- Audit: `import.applied` and `import.rolled_back` (batch id, source, mode, counts; never
  amounts, descriptions or the file name); group "Import and export". No per-row audit events
  for imports (the rows carry `import_batch_id`). Logs never contain row contents.
- `ImportBatch` and `ImportCategoryMapping` follow docs/architecture.md (repository, tenant
  factory, coverage entries); the new columns get migrations; `alembic check` is clean.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.2 from docs/specs/M4-ledger-export-import.md: the pure-ASGI body-size middleware with its two settings,
ImportBatch (+ import_batch_id/import_key columns) with migrations and repositories, upload with the stored file and its
cleanup job, ZIP safety checks, parse/plan in a worker thread with a time limit, the dry-run report (per-month and
per-category totals, closed-month skips, errors CSV), apply in one transaction with re-plan, dedupe (id lineage and
content key with occurrence number), rollback and cancel. Only a trivial importer for tests here; the real importers are
F4.3 and F4.4. Tests: 413 without buffering, every ZIP refusal (zip bomb, traversal, symlink, duplicates), idempotent
re-import, identical rows in one file both imported, rollback incl. the closed-month refusal, tenant isolation.
```

---

## F4.3 Transactions, planned values and categories from CSV, with category mapping

**Story:** I bring my history in: I upload a `transactions.csv` (and `budget_lines.csv`) that I
made from my spreadsheet, map its categories onto mine, check the totals and import it.

**Acceptance**
- Lone files accepted (merge mode, editors/owners): `transactions.csv`, `budget_lines.csv`,
  `categories.csv`. The kind of file is recognised from its header, not its name; `months.csv`
  and `workspace.json` alone are refused ("only part of a full restore"). A ZIP without a
  manifest is refused (the manifest is what makes it an archive).
- Upload options for lone CSV files: delimiter **Detect** (default) / comma / semicolon, and
  decimal separator dot (default) / comma; decimal comma is offered only with semicolon.
  Detection splits the header line on `,` and `;` and picks the one that yields only known
  column names. Files inside an archive always use the canonical format; the options do not
  apply.
- Header names are matched trimmed and case-insensitively; a missing required column is an
  error; unknown columns are a warning and ignored. Values as in the
  [format reference](#format-reference-v10): amounts are converted with the currency's minor
  digits and refused (never rounded) when they have too many decimals, are not positive or are
  too large (F2.2's codes); dates must be `YYYY-MM-DD` within 1900-01-01..2199-12-31; a
  `currency`, when present, must equal the workspace's base currency (row error otherwise);
  descriptions follow F2.2's rules (at most 200 characters, whitespace collapsed, control
  characters refused).
- **Category mapping step** between the dry-run and apply: the source categories (kind + name,
  with row counts and totals) → **map** to an existing category of that kind (active or
  archived: history may belong to a retired category), **create** a new one (name, treatment,
  colour), or **ignore** (its rows are skipped and listed). Defaults: a saved mapping; else the
  same name (F2.1 name key) among active categories of that kind; else "create", proposing
  `treatment=savings` for names in a savings word list (data file, en + pl: savings,
  investments, oszczędności, inwestycje, ...). "Remember these mappings" saves them as
  `ImportCategoryMapping`. Apply is blocked until every source category is resolved.
- Direction must equal the category's kind (mapping is per kind, so it does). Rows of
  `transfer` categories import normally.
- `budget_lines.csv` in merge mode: rows with `planned` zero or empty are skipped silently (zero
  means "no line", as in F2.3); a line for an archived or `transfer` category is skipped and
  listed (F2.3 takes no new lines there); a month that receives a line gets `plan_started_at`
  if it has none (the import counts as the month's first plan write, so F2.3's automatic copy
  never adds to it later).
- `categories.csv` alone (merge): creates the categories that do not resolve (by lineage id, or
  by name key and kind among active ones), appended at the end of their list in file order,
  archived ones as archived; existing categories are never changed (differences listed).
- `transactions.csv` rows with a non-empty `deleted_at` are skipped in merge mode and counted
  ("deleted in the source"); only a restore recreates deleted rows.
- The page offers **template downloads**: `transactions.csv` and `budget_lines.csv` with the
  header and the example rows of the format reference.
- Messages and report labels are translatable (en, pl); column names and enum values
  (`expense`, `income`, `regular`, ...) are format tokens and stay English in every language.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.3 from docs/specs/M4-ledger-export-import.md on the F4.2 framework: lone transactions.csv, budget_lines.csv
and categories.csv import in merge mode, delimiter detection and the semicolon/decimal-comma options, value validation
reusing domain.transactions and domain.budget, the category mapping step with saved mappings and the en/pl savings word
list, closed-month skips, plan_started_at on imported months, template downloads.
Tests (synthetic files only): every validation code, BOM and CRLF, semicolon + decimal comma, mapping defaults and saved
mappings, ignored categories, archived/transfer budget lines skipped, idempotent re-import of a hand-made file.
```

---

## F4.4 Full restore into an empty workspace, and archive merge

**Story:** After a reinstall (or to move to another instance) I restore my workspace from its
archive and everything is back; I can also merge an archive's data into a workspace I already
use.

**Acceptance**
- An archive upload offers two modes:
  - **Restore** (owners only), only when the target workspace is **empty**: no categories
    (archived included), no transactions (deleted included), no budget lines, no month states.
    The check is a static list `services.archive.EMPTINESS_CHECKS` that later milestones extend
    (like `BASE_CURRENCY_LOCKS`). Members, invitations and audit events do not count.
  - **Merge** (editors and owners): data only (categories, transactions, budget lines) through
    the F4.3 rules and mapping step, with categories from `categories.csv` defaulting to
    "create" with their archived attributes when they do not resolve. `workspace.json` and
    `months.csv` are **not** applied; differing settings and the archive's closed months are
    listed as information. A `base_currency` different from the workspace's refuses the whole
    archive.
- **Restore order, in one transaction:** (1) settings from `workspace.json` (name, base
  currency, country, time zone, locale, automatic plan copy; the replaced values are kept in
  `ImportBatch.previous_settings`); (2) categories with every attribute (`kind`, `treatment`,
  `color`, `sort_order`, `archived_at`) and `import_key = id:<id>`; (3) budget lines (lines on
  archived or transfer categories included: they existed in the source); (4) transactions in
  file order (so entry order within a day is kept), soft-deleted ones with their `deleted_at`;
  (5) month states last, exactly as in `months.csv` (`plan_started_at`, `closed_at`; nothing is
  derived from the budget lines). No mapping step, no skipped rows: any error blocks the
  restore.
- Restored closed months get `closed_by_user_id` = the restoring owner (users are not in the
  archive; see open question 4). Restored data is not audited per row; one
  `workspace.restored` audit event (batch id, counts, names of the settings fields changed).
- **Restore rollback** (owners): allowed only while the workspace still holds exactly what the
  restore wrote (no row without the batch's id, no row, month state or setting changed after
  `applied_at`); it then **hard-deletes** the batch's rows and month states and puts
  `previous_settings` back, so the workspace is empty again and a corrected archive can be
  restored. Otherwise it is refused with the reason. (Hard delete is justified: the rows never
  existed in this workspace before, and soft-deleted leftovers would keep the workspace
  non-empty and its base currency locked.)
- Version handling per ADR-0016: an archive of a newer **major** format version is refused
  ("made by a newer version of the app"); a newer **minor** version imports with a warning
  listing the ignored files and columns; older majors pass through the reader's upgrade
  functions (none exist for 1.x).
- Round-trip test: synthetic workspace → export → restore into an empty workspace in the same
  database → export again gives byte-identical data files (`manifest.json` differs only in
  `exported_at` and the app version).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.4 from docs/specs/M4-ledger-export-import.md: archive upload with restore and merge modes, the emptiness check
(EMPTINESS_CHECKS), restore in the specified order incl. settings, archived categories, soft-deleted transactions,
plan_started_at and closed months applied last, restore rollback with its "unchanged since" check and previous_settings,
merge mode reusing F4.3 with settings/closed months reported but not applied, format version handling per ADR-0016.
Tests: the round-trip test, restore refused into a non-empty workspace and for editors, merging an archive into its
source workspace creates nothing, base-currency mismatch refused, newer major refused, restore rollback allowed/refused.
```

---

## F4.5 Documentation: format reference and AI conversion guide

**Story:** I can convert my own spreadsheet into the format without reading code, optionally
with help from an AI assistant, and I understand what that means for my privacy.

**Acceptance**
- `docs/archive-format.md`: the format reference below, kept in sync with `domain.archive` (a
  test parses its example rows with the reader and checks that every column the writer emits is
  documented).
- `docs/import-export.md`: the user guide: exporting; restoring (when it is possible and what it
  restores); merging; importing a lone CSV; the dry-run report and comparing its totals with a
  spreadsheet; category mapping; closed months; rollback; limits; and "Converting a
  spreadsheet" with the ready-to-copy prompt below and the privacy note.
- README "User guides" lists both; docs/workspace-settings.md links the export page;
  docs/deployment.md documents the two body-size settings (F4.2).
- The privacy note is prominent and says: the app never sends your data anywhere; pasting
  financial data into a third-party AI service is your own choice and subject to that service's
  terms; consider a local model, or remove descriptions or other details first; check the result
  with the dry-run totals.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.5 from docs/specs/M4-ledger-export-import.md: docs/archive-format.md and docs/import-export.md from the
"Format reference v1.0" and "AI conversion prompt" sections, README and related doc links, and a test that parses the
documented example rows with domain.archive and checks that every written column is documented.
```

---

## Format reference v1.0

Normative for F4.1–F4.4. Changes follow ADR-0016, and this section changes in the same PR.

### Archive layout

```
assetboard-home-20261018-143000Z.zip
├── manifest.json
├── workspace.json
├── categories.csv
├── budget_lines.csv
├── transactions.csv
└── months.csv
```

All members sit at the root of the ZIP (no folders). Every file is required in an archive, even
when it has only its header row. (The file name prefix comes from `branding.CLI_NAME`.)

### Common rules

- Encoding UTF-8. Export writes no BOM; import removes one if present.
- CSV per RFC 4180: one header row; comma delimiter; `"` quotes fields that contain a comma,
  quote, CR or LF, with inner quotes doubled; export ends lines with CRLF, import accepts CRLF
  or LF. Export writes columns in the order listed; import matches names in any order.
- Dates `YYYY-MM-DD`; months `YYYY-MM`; timestamps UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` (import
  also accepts an explicit offset and converts it to UTC).
- Amounts: plain decimals with `.`, no grouping, no sign, no currency symbol. Export writes
  exactly the currency's minor digits (`12.50` for EUR, `1200` for JPY); import accepts fewer
  (`12.5`) and extra trailing zeros (`12.500`), and refuses more significant decimals.
- An empty optional field means no value. Enum values are lowercase English tokens.
- Lone CSV uploads may instead use `;` as delimiter, and then `,` as decimal separator (chosen
  on the upload form). Files inside an archive never do.

### `manifest.json`

```json
{
  "format": "workspace-archive",
  "format_version": "1.0",
  "app": {"name": "AssetBoard", "version": "0.4.0"},
  "exported_at": "2026-10-18T14:30:00Z",
  "base_currency": "EUR",
  "files": [
    {"path": "workspace.json", "sha256": "9f2c…", "rows": null, "bytes": 161},
    {"path": "categories.csv", "sha256": "0a41…", "rows": 24, "bytes": 2210},
    {"path": "budget_lines.csv", "sha256": "77de…", "rows": 310, "bytes": 18950},
    {"path": "transactions.csv", "sha256": "c3b0…", "rows": 4120, "bytes": 512004},
    {"path": "months.csv", "sha256": "5e19…", "rows": 30, "bytes": 1290}
  ]
}
```

`format` is independent of the product name, so it survives a rename; `app` is informational.
`sha256` is the lowercase hex digest of the member's bytes (shortened above). `rows` excludes
the header and is `null` for JSON files.

### `workspace.json` (restore only)

```json
{
  "name": "Home",
  "base_currency": "EUR",
  "country_code": "PL",
  "timezone": "Europe/Warsaw",
  "locale": "pl",
  "auto_copy_plan": false
}
```

`country_code` may be `null`. Later settings are added as optional keys (a minor version).

### `categories.csv`

| Column | Required | Values |
|---|---|---|
| `id` | in an archive; optional alone | UUID (lineage id) |
| `kind` | yes | `expense` \| `income` |
| `name` | yes | 1–80 characters |
| `treatment` | no (default `regular`) | `regular` \| `savings` \| `transfer` |
| `color` | no (default from the palette) | `#rrggbb` |
| `sort_order` | no (default: file order) | integer ≥ 0, per kind |
| `archived_at` | no | timestamp; set = archived |

Export order: kind (`expense`, `income`), active before archived, `sort_order`, name key, id.

```csv
id,kind,name,treatment,color,sort_order,archived_at
2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,expense,Food,regular,#4f9d69,0,
6a3d0f42-91b8-4e2c-8d0e-7c5b1a2f3e44,expense,Investments,savings,#3b6ea8,1,
d1c9e7a0-3b2f-4a8e-b6c5-0e9f8d7c6b55,expense,Car,regular,#a35d3b,2,2025-12-31T18:05:00Z
91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77,income,Salary,regular,#2f7f5f,0,
```

### `transactions.csv`

| Column | Required | Values |
|---|---|---|
| `date` | yes | `YYYY-MM-DD` |
| `direction` | yes | `expense` \| `income` (= the category's kind) |
| `amount` | yes | decimal > 0 |
| `currency` | no (default: base currency) | must equal the base currency |
| `category` | yes | category name (of the direction's kind) |
| `description` | no | at most 200 characters |
| `category_id` | no (always set in an archive) | UUID from `categories.csv`; needed because archived categories may share a name |
| `id` | no | UUID (lineage id; enables exact dedupe) |
| `deleted_at` | no | timestamp; set = soft-deleted (restored as deleted; skipped in merge) |

Export order: date, then entry order (`created_at`, id). F2.6's export writes the same columns
without `deleted_at`.

Archive example:

```csv
date,direction,amount,currency,category,description,category_id,id,deleted_at
2026-09-01,income,3400.00,EUR,Salary,September salary,91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77,0b6e2f1a-4c3d-4e5f-8a9b-1c2d3e4f5a01,
2026-09-02,expense,50.00,EUR,Food,Groceries,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02,
2026-09-02,expense,4.20,EUR,Food,"Bakery, rolls",2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,7e8f9a0b-1c2d-4e3f-8a4b-5c6d7e8f9a03,2026-09-03T07:12:44Z
```

Hand-made file with the minimal columns (the two identical rows are both imported):

```csv
date,direction,amount,category,description
2025-01-03,expense,12.5,Food,Bakery
2025-01-03,expense,12.5,Food,Bakery
2025-01-31,income,3000,Salary,
```

The same first row with semicolons and a decimal comma (upload options "semicolon" and
"comma"):

```csv
date;direction;amount;category;description
2025-01-03;expense;12,50;Food;Bakery
```

### `budget_lines.csv`

| Column | Required | Values |
|---|---|---|
| `month` | yes | `YYYY-MM` |
| `kind` | no, unless the name exists in both kinds | `expense` \| `income` |
| `category` | yes | category name |
| `planned` | yes | decimal ≥ 0 (0 or empty = no line, skipped) |
| `currency` | no (default: base currency) | must equal the base currency |
| `category_id` | no (always set in an archive) | UUID from `categories.csv` |

Export order: month, kind, category order. At most one row per (month, category).

```csv
month,kind,category,planned,currency,category_id
2026-09,expense,Food,550.00,EUR,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01
2026-09,income,Salary,3000.00,EUR,91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77
```

### `months.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `month` | yes | `YYYY-MM` |
| `closed_at` | no | timestamp; set = month closed |
| `plan_started_at` | no | timestamp (F2.3's automatic-copy marker) |

One row per stored month state; a month without a row is open and has no marker.

```csv
month,closed_at,plan_started_at
2026-08,2026-09-02T19:40:00Z,2026-07-28T08:00:00Z
2026-09,,2026-09-01T06:30:12Z
```

### Not in the archive (format 1.0)

Members, invitations, users and their sessions, the audit log, import batches and saved
mappings, and who created or closed what. Data of later milestones (M3, M5–M8) is added by their
archive-extension features as new files (minor versions).

---

## AI conversion prompt

Published in docs/import-export.md by F4.5. Users paste it into an AI assistant of their
choice together with their data; the app itself never calls an AI or any external service.

> **Privacy:** sending your financial data to a third-party AI service is your own choice and
> is governed by that service's terms. Consider a local model, or remove descriptions and other
> details you would rather not share. Always check the result with the import's dry-run totals.

```text
Convert my personal budget spreadsheet (pasted or attached below) into CSV files for a budgeting
app. Output only the files described here, each in its own code block, then the three lists at
the end.

File 1: transactions.csv, one row per income or expense entry.
- Header, exactly: date,direction,amount,category,description
- date: YYYY-MM-DD. If an entry only has a day number, take the year and month from the
  sheet, tab or section it is in (for example a tab named "08.26" means August 2026). If you
  cannot tell the date, do not guess: list the entry under "Questions".
- direction: expense or income (lowercase).
- amount: a positive number with a dot as decimal separator, at most 2 decimals, no thousands
  separators, no currency symbol. Never output a negative or zero amount; if my sheet has a
  negative value, list it under "Questions" (refund or income?).
- category: my category name exactly as it is written in my sheet, spelled the same way every
  time. Do not invent, merge or translate categories.
- description: my description text, at most 200 characters; leave it empty if there is none.
- Put a field in double quotes if it contains a comma, a double quote or a line break, and
  double any double quote inside it.
- Keep repeated entries: two identical purchases on the same day are two rows.
- Skip totals, subtotals, summary blocks, headers, empty rows and error values such as #REF!.

File 2 (only if my sheet has planned or budget values): budget_lines.csv
- Header, exactly: month,kind,category,planned
- month: YYYY-MM. kind: expense or income. planned: a positive number formatted like amount.
- Use the same category names as in transactions.csv. Skip zero or empty planned values.

Rules for both files: UTF-8, comma as delimiter, one header row, no extra columns, no comments.

After the files:
1. "Check table": for every month, the total of expense amounts, the total of income amounts
   and the total planned, as computed from the files you produced.
2. "Skipped": every row you left out and why.
3. "Questions": anything you could not convert without guessing.
```

---

## Open questions (for the owner)

1. **Viewer export.** Proposed here: owners and editors only. Viewers already see every amount
   on screen, and "your data is yours" argues for letting them export too; but an archive makes
   bulk copying trivial for someone given read-only access. Allow viewers?
2. **Soft-deleted transactions in the archive.** Decided here: included (with `deleted_at`) for
   restore fidelity: they lock the base currency and keep categories "in use" (F2.1, F2.2), and
   a later trash view can restore them. Should the export offer "leave out deleted entries" (such
   an archive then restores with slightly different behaviour)?
3. **Attachments** (receipts, documents), if they ever exist: in the ZIP under an
   `attachments/` folder (an exception to the no-folders rule, larger limits), or outside the
   archive?
4. **Who closed a month after a restore.** Decided here: the restoring owner (users are not in
   the archive). Alternative: leave it empty, shown as "by a former member".
5. **Restore into a new workspace.** Existing users cannot create another workspace yet, so a
   restore needs an empty workspace (a fresh install or a new registration). Add "Create a
   workspace from an archive" (proposed for the post-1.0 backlog)?
6. **CLI.** Should `assetboard workspace export|import` exist (for scheduled off-site exports),
   or is F9.4's database backup enough for automation?
7. **CSV formula injection in F2.6's spreadsheet-oriented export.** The archive is lossless and
   never prefixes values starting with `=`, `+`, `-` or `@`; should F2.6 offer that protection
   (which then breaks exact re-import of such descriptions)?
8. **Rollback of rows edited after the import.** Decided here: a merge rollback soft-deletes
   every transaction of the batch, edited ones included (the confirm page says how many were
   edited). Keep edited ones instead?
9. **Order of M3 and M4.** M4 now depends only on M2 and could ship before M3; if M3 ships first,
   its archive extension (F3.8) must land before M4 counts as a full backup.
