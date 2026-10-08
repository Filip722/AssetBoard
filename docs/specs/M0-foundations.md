# M0 — Foundations

**Goal:** an empty but production-shaped app: repo tooling, config, DB layer with money types, UI shell with i18n, Docker image, CI. No business features.

**Exit criteria:** `docker compose up` on a clean machine serves the app shell at `/`, `/healthz` is OK, data persists across restarts, CI is green, `make check` works locally.

Every agent prompt below starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

---

## F0.1 Repository scaffold and tooling

**Story:** As the maintainer, I clone the repo, run two commands, and have a working dev environment with linting, typing and tests.

**Acceptance**
- `make setup && make check` passes on a fresh clone (Linux/macOS).
- `pyproject.toml`: package `AssetBoard` under `src/`, Python `>=3.13`, license `AGPL-3.0-or-later`, dependency groups `dev`/`test`; `uv.lock` committed; `.python-version` committed.
- Layout: `src/assetboard/{domain,services,db,web,api,jobs,i18n}`, `src/assetboard/branding.py`, `tests/{unit,integration}`, `docs/{specs,adr}`, `docker/`, `scripts/`.
- ruff (lint + format), pyright (strict for `domain/` and `services/`, basic elsewhere), pytest with coverage report, pre-commit hooks (ruff, format, trailing whitespace, large-file check, **block `*.xlsx` and `*.db` files**).
- `.gitignore` covers Python, `.env`, `*.db`, `*.xlsx`, `data/`.
- `LICENSE` is the AGPL-3.0 text; README states the license.
- `.github/workflows/ci.yml` runs `make check` on push and PR.

**Notes:** `branding.py` holds `APP_NAME`, `ENV_PREFIX`, `CLI_NAME`. A trivial test imports every package to catch circular imports.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement feature F0.1 from docs/specs/M0-foundations.md: repository scaffold and tooling.
Use uv, ruff, pyright, pytest, pre-commit and a Makefile with the targets listed in AGENTS.md
(setup, dev, check, test, css, migrate, revision, i18n-*; targets for later features may print "not yet implemented").
Create the directory layout from the spec, branding.py, CI workflow, and pre-commit config that blocks *.xlsx and *.db.
Do not add any web framework code yet.
```

---

## F0.2 Application skeleton and configuration

**Story:** The app starts from one command and is configured entirely via environment variables.

**Acceptance**
- `create_app(settings)` app factory (FastAPI); `uvicorn` entrypoint; Typer CLI `AssetBoard` with `serve` and `migrate`.
- `Settings` (pydantic-settings, prefix from `branding.ENV_PREFIX`): `DATA_DIR` (default `/data` in Docker, `./data` in dev), `DATABASE_URL` (default SQLite in `DATA_DIR`), `SECRET_KEY` (if absent: generate once, persist to `DATA_DIR/secret_key`, log a warning), `BASE_URL`, `ENV` (dev|prod), `LOG_LEVEL`, `LOG_FORMAT` (text|json), `TRUSTED_PROXIES`, `DEFAULT_LOCALE`, `DEFAULT_CURRENCY`, `AUTO_MIGRATE` (default true).
- `Clock` abstraction (`now()`, `today(tz)`), with a fixed-clock fake for tests.
- Request-ID middleware; structured logging; `/healthz` (process up) and `/readyz` (DB reachable).
- Settings are validated at startup with clear error messages for bad values.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F0.2 from docs/specs/M0-foundations.md: app factory, Settings, Clock, logging, request-id middleware,
/healthz and /readyz, Typer CLI (serve, migrate).
Keep names in branding.py. Include tests for settings parsing/validation and the health endpoints.
Do not create database models yet.
```

---

## F0.3 Database foundation

**Story:** A typed, migratable DB layer with correct money and decimal handling.

**Acceptance**
- SQLite engine with pragmas: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout`, `synchronous=NORMAL`.
- SQLAlchemy 2.0 `Base` with an Alembic-friendly naming convention; mixins: `IdMixin` (UUID), `TimestampMixin`, `SoftDeleteMixin`, `WorkspaceScopedMixin` (no repository logic yet).
- `Money` value object (domain) + helper to map `(amount_minor, currency)` columns via `composite`. `DecimalType` (TEXT on SQLite, `NUMERIC(38,18)` on PostgreSQL) that round-trips `Decimal("0.00924")`, 18-decimal values, negatives and rejects floats.
- Alembic configured from `Settings`; `make migrate`, `make revision m=…`; CI runs `alembic check` and `upgrade head` on an empty DB.
- Test fixtures: migrated SQLite DB per test session, transactional rollback per test, injected fixed `Clock`.
- A throwaway test model proves the mixins and types (removed or kept under `tests/`).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F0.3 from docs/specs/M0-foundations.md: DB engine with SQLite pragmas, declarative Base with naming convention,
mixins, Money value object, DecimalType (TEXT on SQLite / NUMERIC(38,18) on PostgreSQL), Alembic setup, pytest DB fixtures.
Hard rules: no floats anywhere; Money is integer minor units + ISO currency code; DecimalType must reject float input.
Write property-based tests (hypothesis) for Decimal round-tripping and Money arithmetic/rounding (half up).
```

---

## F0.4 Web shell, templating and i18n

**Story:** The app has a consistent, responsive, translatable UI shell.

**Acceptance**
- Jinja2 environment with gettext extension; Babel config; `locales/en`, `locales/pl`; `make i18n-extract|update|compile`; locale resolution: user setting → workspace default → `Accept-Language` → `en`.
- Base layout: top bar, left navigation with placeholders for Month, Recurring, Net worth, Investments, Loans, FIRE (disabled until implemented), user menu; responsive down to ~375 px; dark mode (class-based, persisted in `localStorage`).
- Tailwind built via standalone CLI (pinned version) through `make css`; HTMX and Alpine.js (**CSP build**) and ECharts vendored under `static/vendor/` with pinned versions; **no CDN calls at runtime**.
- Components (Jinja macros): form field with error display, flash messages, confirm dialog, toast with undo (HTMX OOB swap), empty state, money display.
- Number helpers `parse_amount(text, locale)` / `format_money(Money, locale)` with tests: `"1 234,56"`, `"1.234,56"`, `"1,234.56"`, `"12,5"`, `"-3"`, invalid input → clear error.
- Error pages 404/403/500, HTMX-aware (partial error responses).
- Strict security headers (CSP without `unsafe-inline`/`unsafe-eval`, `X-Content-Type-Options`, `Referrer-Policy`).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F0.4 from docs/specs/M0-foundations.md: Jinja2 base layout + nav placeholders, Tailwind standalone build,
vendored HTMX/Alpine(CSP build)/ECharts, Babel i18n (en source, pl catalog), Jinja macros listed in the spec,
locale-aware amount parsing/formatting helpers, error pages, security headers incl. a strict CSP.
All strings must be translatable. Tests: number parsing/formatting table tests, locale resolution, CSP header present.
No runtime CDN requests.
```

---

## F0.5 Container and CI/CD

**Story:** I can run the app in my homelab with one compose file, and releases publish images automatically.

**Acceptance**
- Multi-stage Dockerfile (uv build → slim runtime), non-root user (uid 10001), `/data` volume, `HEALTHCHECK` on `/healthz`, OCI labels, works with read-only root filesystem when `/data` and `/tmp` are writable.
- `compose.example.yaml` with volume, env examples, healthcheck, restart policy, and commented reverse-proxy (Traefik/Caddy) labels.
- `docker.yml`: build on PRs (no push); on tag `v*` build **linux/amd64 + linux/arm64** with buildx and push to GHCR with semver tags + `latest`.
- `release-please` config (Conventional Commits → changelog + version PR); `dependabot.yml` for pip, github-actions, docker.
- Smoke test in CI: start the container, wait for `/healthz`.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F0.5 from docs/specs/M0-foundations.md: multi-stage Dockerfile with uv, non-root user, /data volume, HEALTHCHECK;
compose.example.yaml; GitHub Actions for docker build (PR: build only; tag: multi-arch push to GHCR);
release-please and dependabot configs; a CI smoke test that boots the container and polls /healthz.
Pin base image tags/digests where practical. Document run instructions in README.
```

---

## F0.6 Docs, governance and ADRs

**Acceptance**
- README: what/why, status "pre-alpha", quickstart (compose), configuration table, roadmap link.
- `CONTRIBUTING.md` (setup, workflow, commit style, spec-driven development), `SECURITY.md` (private reporting via GitHub advisories), `CODE_OF_CONDUCT.md` (Contributor Covenant), issue templates (bug, feature with "Spec ID" + acceptance checklist), PR template (assumptions, deviations, acceptance↔test table).
- ADR template and ADRs: 0001 stack, 0002 money & decimals, 0003 multi-tenancy (workspaces), 0004 server-rendered UI, 0005 license AGPL-3.0-or-later and `branding.py`, 0006 UUID primary keys.
- `docs/specs/` from this pack committed; GitHub milestones M0–M9 created; one issue per feature ID (script optional).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F0.6 from docs/specs/M0-foundations.md: README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue/PR templates,
ADR template and ADRs 0001–0006 (derive content from AGENTS.md and docs/specs/00-overview.md; keep each ADR to
context / decision / consequences). Optionally add scripts/create_github_issues.py that reads feature headings
from docs/specs/M*.md and prints `gh issue create` commands (dry-run by default).
```
