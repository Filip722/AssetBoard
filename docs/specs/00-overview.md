# AssetBoard — product & technical overview (working title)

Status: draft v1. This file is the shared "constitution". Milestone specs (`M0`…`M9`) refer to it.

## 1. Vision and principles

A self-hosted web app that replaces a personal finance spreadsheet (monthly income/expense sheets with planned-vs-actual summaries, net worth, investment portfolio, mortgage calculator, FIRE projection) and adds what spreadsheets cannot do: recurring items with reminders, variable income, optional tax modelling, multi-user + SSO, history/snapshots, backups.

1. **Spreadsheet-fast data entry.** Adding a transaction is a keyboard-driven inline row, not a modal form.
2. **Optional complexity.** Taxes, recurring items, loans, FIRE, multi-currency, work logs: all can be ignored; the app works with just categories and transactions.
3. **Correctness first.** Finance math lives in pure functions with golden tests against independent oracles.
4. **Self-hosting first.** One container, SQLite, no required external service. Online features (price feeds, notifications, SSO) degrade gracefully when unavailable.
5. **Your data is yours.** Full export, documented schema, no telemetry.
6. **Boring technology.** Fewer moving parts beats cleverness.

## 2. Glossary

- **Workspace**: a household/budget universe. All financial data belongs to exactly one workspace.
- **Member / role**: a user's membership in a workspace: `owner`, `editor`, `viewer`.
- **Category**: label for transactions. Has a `kind` (expense | income) and a `treatment` (regular | savings | transfer).
- **Planned / actual / difference**: budget per category per month vs. sum of transactions; see §3.5.
- **Scheduled item**: a recurring rule (insurance, rent, subscription). **Occurrence**: one due instance of it.
- **Income source**: a fixed or variable income (e.g., paid per day worked).
- **Tax rule**: optional, user-defined tax/levy (name, percent or fixed) applied to an income source.
- **Net-worth item**: asset or liability with manual or linked valuation.
- **Instrument / holding / lot**: a security or asset type / what you currently hold / a single buy.
- **Snapshot**: a stored point-in-time total (net worth) used for history charts.

## 3. Shared rules

### 3.1 Workspaces and roles
- Every workspace-owned table has `workspace_id` (non-null, indexed, FK). No exceptions.
- `viewer`: read-only. `editor`: create/update/delete data. `owner`: plus members, workspace settings, deletion.
- A user may belong to several workspaces; the UI has a workspace switcher. Single-user installs simply have one.

### 3.2 Money, quantities, currency
- Money columns are `(amount_minor BIGINT, currency CHAR(3))`. Domain type `Money`. Amounts stored positive; direction comes from the entity (income/expense, asset/liability).
- Workspace has a `base_currency` (default EUR). Reports and totals are in base currency. v1 ledger entries are entered in base currency; the schema carries `currency` everywhere so multi-currency can be added without migrations of meaning.
- Quantities and unit prices use `DecimalType`: TEXT on SQLite, `NUMERIC(38,18)` on PostgreSQL. Because SQL aggregates on TEXT are unreliable, **aggregate quantities/prices in Python (domain layer)**, not in SQL.
- Rates in integer basis points.

### 3.3 Categories and treatment
- `kind=expense, treatment=regular`: spending. `treatment=savings`: money moved to savings/investments (counted as "saved", not "spent"). `treatment=transfer`: between own accounts (excluded from both).
- `kind=income, treatment=regular`: earnings. `treatment=savings`: withdrawal from savings (excluded from "earned").
- Derived KPIs: `earned`, `spent`, `saved`, `savings_rate = (earned - spent) / earned` (guard divide-by-zero).
- Categories are flat in v1; a `parent_id` column will be added when a category hierarchy is implemented (not in F2.1). Archiving hides a category from entry but keeps history.

### 3.4 Months
- A "month" is a calendar month in the workspace timezone. Month pages are created lazily; nothing is "copied" like spreadsheet tabs.
- Budget (planned) lines are per (month, category). Opening a month with no budget lines offers/does carry-forward from the previous month.
- A month can be **closed** (read-only for editors) to prevent accidental edits; owners can reopen.

### 3.5 Variance convention
Positive variance is always *favorable*:
- Expenses: `difference = planned - actual` (positive = under budget).
- Income: `difference = actual - planned` (positive = more than planned).

### 3.6 `PercentOrFixed` value object
Used by tax rules, loan cost components and similar. `mode: percent | fixed`; `percent` stores basis points, `fixed` stores Money. One domain class, one set of tests, one form widget.

### 3.7 Linked valuations
A net-worth item is either `manual` (user enters value; every change is appended to a valuation history) or `linked` (value computed from another module: asset class in portfolio, a single holding, a loan's remaining principal). Links are by `(link_type, link_id)`; resolution lives in the owning module's service.

### 3.8 History and snapshots
Anything the user will want a trend of is stored historically: manual valuations (append-only), price points, FX rates, net-worth snapshots (monthly job + manual "take snapshot"), loan balance checkpoints.

### 3.9 Audit and deletion
- Soft delete (`deleted_at`) for user-entered financial rows; UI offers undo for ~10 s and a trash view later.
- `AuditEvent(workspace_id?, actor_user_id, action, entity_type, entity_id, data JSON, ts)` written by services for create/update/delete of financial data and for auth/admin events.

### 3.10 IDs and time
UUID primary keys (`Uuid` type). Dates are `DATE`; timestamps UTC. "Today" always comes from the injected `Clock` in the workspace timezone.

## 4. Domain model by milestone (key entities, not final DDL)

| Milestone | Entities |
|---|---|
| M1 | User, ExternalIdentity, UserSession, Workspace, Membership, Invitation, AuditEvent |
| M2 | Category, Transaction, BudgetLine, MonthState |
| M3 | ScheduledItem, ScheduledOccurrence, IncomeSource, WorkLog, (WorkDay), TaxRule, TaxRuleVersion, IncomeSourceTaxRule, NotificationChannel, NotificationLog |
| M4 | ImportBatch (+ `import_batch_id` on Transaction) |
| M5 | NetWorthCategory, NetWorthItem, ItemValuation, NetWorthSnapshot |
| M6 | AssetClass, Instrument, InstrumentAllocation, InvestmentTransaction, PricePoint, FxRate, PriceSourceConfig, BondTerms |
| M7 | Loan, LoanRateChange, ReferenceRate, ReferenceRateValue, LoanCostComponent, LoanOverpayment, LoanPayment, LoanBalanceCheckpoint, LoanScenario, InstallmentPlan |
| M8 | FireScenario, FireContribution, FireEvent |
| M9 | ApiToken, BackupRun, DashboardPreference |

Key fields for the shared entities:
- **Transaction**: id, workspace_id, direction (income|expense), date, amount (Money), category_id, description, source (manual|recurring|recurring_auto|import), scheduled_occurrence_id?, import_batch_id?, account_id? (reserved), created_by, timestamps, deleted_at.
- **BudgetLine**: workspace_id, month (first day), category_id, planned (Money); unique (workspace, month, category).
- **Category**: workspace_id, name, kind, treatment, color, sort_order, archived_at.

## 5. Roadmap and dependencies

| Milestone | Theme | Depends on | Release tag |
|---|---|---|---|
| M0 | Foundations (repo, CI, Docker, DB, UI shell, i18n) | — | v0.0.x |
| M1 | Identity: local auth, workspaces, OIDC SSO | M0 | v0.1 |
| M2 | Ledger core: categories, transactions, month view, planned vs actual | M1 | v0.2 |
| M3 | Recurring items, variable income, taxes, reminders | M2 | v0.3 |
| M4 | Excel importer (dogfooding point: you can leave the spreadsheet for monthly tracking) | M2 (M3 for work logs) | v0.4 |
| M5 | Net worth: assets, debts, snapshots | M1 | v0.5 |
| M6 | Investments | M5 | v0.6 |
| M7 | Loans and mortgage | M5, M3 | v0.7 |
| M8 | FIRE planner | M2, M6, M7 | v0.8 |
| M9 | Dashboard, reports, export/backup, API, hardening | all | v1.0 |

Each of M5–M8 ends with a feature "importer extension" that adds that sheet to the Excel importer.

## 6. Non-goals (v1)
Bank sync/aggregation; receipt OCR; tax filing; advice of any kind; native mobile apps (a responsive PWA is enough); built-in country tax tables (tax rules are user-defined; labelled example templates may come later).

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Scope creep | Milestone gates; "optional complexity" principle; follow-ups go to the backlog, not the PR |
| Finance math errors | Pure domain functions, golden tests against oracles, property tests |
| Tenant data leaks | Scoped repositories + automated isolation test over all models |
| Fragile price feeds / ToS | Provider interface, manual fallback, ADR per provider before implementation |
| OIDC account takeover | Link by verified email only if explicitly enabled; PKCE; tests with fake IdP |
| SQLite limits | WAL + single-writer is fine at household scale; stay portable to PostgreSQL |
| Renaming later | All naming in `branding.py` |
| Users mistake tax modelling for advice | Disclaimer in UI and docs; rates are user-entered |

## 8. Workflow with coding agents

Specs live in the repo. Each **feature** (e.g. `F2.2`) is one GitHub issue and one PR. Every feature has an *Agent prompt*. Use this loop:

**Step 1 — Plan.** Paste the feature's Agent prompt. It begins by asking for a plan only:
> Do not write code yet. Reply with (1) your understanding in ≤5 lines, (2) files to create/change, (3) schema/migration changes, (4) test list, (5) assumptions and questions. Wait for my "go".

**Step 2 — Implement.** Answer questions, correct the plan, then say "go". The agent implements, runs `make check`, and finishes with a table mapping each acceptance criterion to the test that proves it.

**Step 3 — Review.** In a fresh agent session (or the same one) paste:
> Review the diff against feature <ID> acceptance criteria and AGENTS.md. List violations, missing tests, tenant-isolation risks, float usage, hardcoded strings, N+1 queries, missing migrations. Fix what you find, then re-run `make check`.

**Step 4 — Merge.** You read the diff yourself (at least the domain/service code and tests), squash-merge, let release-please do the rest.

Rules of thumb: one feature per PR; if an agent session grows past ~1 hour or touches more than ~15 files, split the feature.

## 9. Spec change protocol
If implementation reveals a spec problem, change the spec in the same PR and mention it in the PR description. Milestone specs for later milestones are deliberately detailed but not frozen: re-read and adjust them before starting each milestone.
