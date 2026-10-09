# AGENTS.md — standing instructions for coding agents

Project: **AssetBoard** (working title; the name may change — see "Branding" below).
A self-hosted personal-finance web app for homelabs: monthly income/expense tracking with
planned-vs-actual, recurring items, variable income, net worth, investments, loans/mortgage and FIRE projection.

## Read first, every time
1. `docs/specs/00-overview.md` — principles, shared rules, domain model, workflow.
2. The milestone spec you were pointed to (`docs/specs/M*-*.md`). Implement **only** the feature ID you were given.

## Stack (do not change without an ADR in `docs/adr/`)
- Python 3.13+, `uv` for environments and locking.
- FastAPI, Pydantic v2, SQLAlchemy 2.0 (typed `Mapped[]` style), Alembic.
- SQLite (WAL) by default; code must stay portable to PostgreSQL (no SQLite-only SQL in services).
- Server-rendered UI: Jinja2 + HTMX + Alpine.js (CSP build) + Tailwind (standalone CLI, no Node). Charts: Apache ECharts, vendored.
- Tooling: ruff (lint+format), pyright, pytest, pre-commit, Babel (i18n), APScheduler (in-process jobs), Apprise (notifications), Authlib (OIDC), argon2-cffi.
- **No new runtime dependency without justification in the PR description** (what it does, license, maintenance status, why stdlib/existing deps are not enough). Licenses must be compatible with AGPL-3.0-or-later.

## Architecture rules
- Layers, top to bottom: `web/` (routes, templates, form handling) → `services/` (use cases) → `db/` (models, repositories). `domain/` holds **pure** logic (no I/O, no DB, no clock access): money, taxes, recurrence, amortization, portfolio math, XIRR, FIRE projection.
- Routes never touch ORM models or sessions directly; they call services. Services receive a `Context(user, workspace, role, clock)`.
- Every query on workspace data goes through a repository that **enforces `workspace_id`**. Never write an unscoped query on a workspace-owned table.
- ORM bulk `update()` operations bypass the timestamp listener; do not use them for timestamped rows unless timestamps are explicitly set in the update.
- Alembic's `env.py` imports `assetboard.db.models` so every ORM model is registered in `Base.metadata` before migrations run.
- Authorization (role checks) happens in the service layer, not only in routes.
- Time: never call `datetime.now()` / `date.today()` in services or domain. Use the injected `Clock`.
- External I/O (price feeds, SMTP, OIDC, notifications) sits behind interfaces with fakes for tests. **Tests must never use the network.**

## Money and numbers
- **Never use `float` for money, quantities or prices.** Money = `Money(amount_minor: int, currency: str)`. Quantities and unit prices = `Decimal` via the project `DecimalType`.
- Never apply `ORDER BY` or SQL aggregates to `DecimalType` columns; order or aggregate these values in Python.
- Rates are stored as integer basis points (1 bp = 0.01%), e.g. 23% VAT = `2300`.
- Rounding: round half up to the currency's minor unit, applied once per computed line, never on intermediate sums unless the spec says so.
- Parse/format user-entered numbers with the locale helpers (`"1 234,56"`, `"1,234.56"`, `"12,5"` must work). No ad-hoc `float(str)`.

## Security and privacy
- CSRF protection on every state-changing HTML form; SameSite=Lax HttpOnly cookies; argon2id password hashing.
- Validate all input with Pydantic/forms; escape output (Jinja autoescape stays on); no inline scripts (CSP-ready).
- Never log secrets, tokens, passwords, full request bodies or financial amounts at INFO level or above.
- Secrets stored in the DB (notification URLs, API tokens) are encrypted or hashed per spec.
- No telemetry, no external calls at runtime except features the user explicitly configures (price providers, OIDC, notifications).
- Never commit real personal data. Test fixtures are synthetic.

## i18n
- All user-facing strings go through gettext (`_()` / `{% trans %}`). English is the source language; Polish (`pl`) is the first translation. No hardcoded UI strings in templates or Python.

## Testing
- Every service function and domain function has tests. Domain logic: table-driven tests plus property-based tests (`hypothesis`) where invariants exist.
- Finance math needs **golden tests** against an independent oracle named in the spec.
- Every workspace-owned model is covered by the automated tenant-isolation test (see M1.F1.2).
- A feature is not done if `make check` fails.

## Commands (created in M0.F0.1; keep them working)
`make setup` · `make dev` · `make check` (ruff, pyright, pytest, `alembic check`) · `make test` · `make css` · `make migrate` · `make revision m="message"` · `make i18n-extract` · `make i18n-update` · `make i18n-compile`

## Definition of Done (every feature)
1. All acceptance criteria in the spec are met and each is traceable to a test.
2. `make check` is green; migrations are included and reversible where practical.
3. UI strings are translatable; `pl` catalog updated (machine-translate placeholders are acceptable, mark them fuzzy).
4. Docs updated: the spec (if reality diverged), user docs under `docs/`, `CHANGELOG` entry via Conventional Commit messages.
5. PR description lists assumptions, deviations from the spec, and follow-ups.

## Working agreement
- Plan first, then wait for approval (see workflow in the overview §8).
- If the spec is ambiguous or seems wrong, **stop and list the question/assumption** instead of silently guessing. If you change behaviour, update the spec in the same PR.
- Stay in scope. Note ideas as follow-ups; do not implement them.
- Small, reviewable commits using Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).

## Branding
The app name may change. Keep every occurrence of the name in `src/assetboard/branding.py` (display name, env prefix, CLI name, user-agent), `pyproject.toml`, and Docker labels. Do not hardcode the name in templates or messages — use `branding.APP_NAME`.
