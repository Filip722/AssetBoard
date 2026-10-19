# ADR-0016: Workspace archive format

**Status:** Proposed
**Date:** 2026-10-18

## Context

"Your data is yours" (overview §1) needs a full export of a workspace that can also be restored.
Users also need a way to bring years of history from their own spreadsheets. The first plan was
an importer for one particular workbook layout (M4 "Excel importer", with extensions in M5–M8),
but every spreadsheet differs, it needed `openpyxl`, and it could not serve as a backup. A
separate full export (F9.3) would have been a second format to maintain.

The format must:

- be one format for export, import and restore, readable without the app;
- be easy to produce by hand, with a script or with an AI assistant from any spreadsheet;
- restore a workspace with full fidelity (archived categories, soft-deleted rows, closed months,
  settings);
- be safe to read from an untrusted upload, with the standard library only;
- survive app upgrades and a product rename.

## Decision

- **Container.** A ZIP archive with every member at its root: `manifest.json`,
  `workspace.json` (settings) and one CSV file per entity (`categories.csv`,
  `budget_lines.csv`, `transactions.csv`, `months.csv` in format 1.0). Read and written with
  `zipfile`, `csv`, `json` and `hashlib` only. The normative reference is in
  [the M4 spec](../specs/M4-ledger-export-import.md#format-reference-v10) (published as
  `docs/archive-format.md` by F4.5).
- **CSV conventions.** UTF-8 (BOM tolerated on import), RFC 4180 quoting, comma delimiter,
  dates `YYYY-MM-DD`, UTC timestamps with `Z`, amounts as plain decimals with `.` and the
  currency's minor digits, fixed English column names and enum tokens. Lone CSV uploads may use
  `;` and a decimal comma, chosen on the upload form; archive members never do.
- **Manifest.** `format` (`"workspace-archive"`, independent of the product name),
  `format_version` (`"MAJOR.MINOR"`), `app` (name and version, informational), `exported_at`,
  `base_currency`, and for each file its path, SHA-256, row count and size. Checksums and row
  counts are verified before anything is planned.
- **References.** Rows carry a lineage `id` (the UUID the row had where it was first created);
  related files reference it (`category_id`) and also carry the human name (`category`), so a
  hand-made file can use names alone. Restored rows get new primary keys and keep the lineage in
  `import_key`, so ids stay stable across export → restore → export and a merge into the source
  workspace recognises its own rows.
- **Versioning and compatibility.**
  - A **minor** version only adds: new optional columns, new optional keys in JSON files, new
    files. A reader ignores what it does not know and lists it as a warning.
  - A **major** version is any other change (renamed, removed or reinterpreted columns, new
    required data). The app writes only the current version, reads every major version it has
    ever written by upgrading older archives with pure, tested functions in `domain.archive`
    (one per step), and refuses a newer major version with a clear message.
  - Every format change updates the reference in the same PR, and golden archives of every
    released version stay in `tests/fixtures/` as regression tests.
  - Each later milestone adds its data as new files (a minor version) in an "archive extension"
    feature; a coverage test fails when a workspace-owned model is neither archived nor
    excluded with a reason.
- **Not in the format:** members, users, sessions, the audit log, import batches, and (from
  F9.3) secrets unless explicitly included and encrypted. The archive itself is not encrypted:
  it is a file the user downloads and keeps.

## Consequences

- One format serves backup, migration between instances and "bring my history"; F2.6's CSV
  export uses the same transaction columns, so it re-imports.
- Users convert their spreadsheets themselves (a documented AI prompt helps); the app has no
  layout-specific importer and no `openpyxl` dependency, and never calls external services.
- CSV keeps the format readable in any spreadsheet, at the cost of typing every value as text:
  the reader validates every field, and exact amounts depend on the documented decimal rules.
- Lineage ids cost a nullable `import_key` column (unique per workspace) on archived entities.
- Readers must keep upgrade code for old majors indefinitely; minor additions are cheap, so
  breaking changes should be rare and batched.
- Restore needs an empty workspace; restoring next to existing data is a merge with reported
  conflicts, not an overwrite.
- Alternatives rejected: one JSON file per entity (harder to produce from a spreadsheet, no
  re-use of F2.6), a copy of the SQLite database (F9.4 covers whole-instance backups; not
  per-workspace, not portable to PostgreSQL, not hand-made), an Excel workbook (a new
  dependency and layouts), and a generic column-mapping CSV importer (useful, but post-1.0).
