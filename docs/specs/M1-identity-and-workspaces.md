# M1 — Identity and workspaces

**Goal:** secure multi-user foundation before any financial feature exists, so tenant scoping is never retrofitted.

**Exit criteria:** first-run setup creates an admin + workspace; users can log in locally or via OIDC; roles are enforced; an automated test proves no cross-workspace data access for every workspace-owned model. TOTP MFA is explicitly out of scope for M1 and is moved to the [post-1.0 backlog](backlog-post-1.0.md); use OIDC or reverse-proxy auth backed by an IdP that supports MFA until then.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `User(id, email unique ci, display_name, password_hash?, locale, theme, is_active, is_instance_admin, created_at, last_login_at)`
- `ExternalIdentity(user_id, provider, subject, email_at_link, created_at)`; unique `(provider, subject)`
- `UserSession(id_hash, user_id, created_at, last_seen_at, expires_at, user_agent, ip_prefix)`
- `Workspace(id, name, base_currency, locale, timezone, country_code, created_at)`
- `Membership(workspace_id, user_id, role)`; unique `(workspace_id, user_id)`
- `Invitation(id, workspace_id, token_hash, role, email?, expires_at, used_at?, created_by)`; `email` is required for every new invitation since pre-release A (NULL only on older rows)
- `AuditEvent(...)` as defined in the overview §3.9

---

## F1.1 Local accounts, sessions and first-run setup

**Story:** On first start I create the admin account and my workspace; afterwards I log in with email + password.

**Acceptance**
- With zero users, every route redirects to `/setup` (create admin: email, display name, password; workspace: name, base currency, country, timezone, locale). `/setup` returns 404 once a user exists.
- Passwords: argon2id; minimum length 8 (10 until 0.2.0), no composition rules; check against a small bundled common-password list.
- Sessions are **server-side** (`UserSession`), cookie holds an opaque random id (stored hashed), `HttpOnly`, `SameSite=Lax`, `Secure` when `BASE_URL` is https; idle timeout 14 days, absolute 60 days (configurable); "log out other devices".
- Login/logout, change password (invalidates other sessions), CSRF on all forms.
- Login throttling per IP and per account (e.g., 5 failures → exponential backoff), generic error messages (no user enumeration), constant-time-ish comparison path for unknown users. Amended 2026-10-17 (ADR-0012): unknown email and wrong password share one message; only an attempt refused by a blocked bucket says "Too many attempts. Try again in N minutes." (401 with `Retry-After`). The per-account (sign-in) bucket is keyed by the normalized email, counted identically for unknown emails, and decays with time only (fixed window equal to the per-IP window, 60 min by default): every attempt counts, successful ones included, and nothing resets it (an attempt is taken back only when no verify outcome exists, e.g. a busy hashing gate). Wrong current passwords on change-password and failed password confirmations (profile, admin, link) count in a separate per-user `confirm` bucket (5 free, same backoff, reset by a correct password) and never touch the sign-in bucket. Decided in PR A: per-account 5 free failures, reset on success (amended 2026-10-17: no reset, time window instead; see below); per-IP (IPv4 address / IPv6 /64, hashed) 20 free failures in a time window that decays only with time; backoff doubles from 60 s, capped at 15 min. Registration has its own per-IP limit (5 attempts per hour, success or not) and never resets a login counter. Wrong current passwords on change-password count against the same per-account bucket (amended 2026-10-17: a separate per-user bucket). IPv4-mapped IPv6 client addresses are treated as IPv4. Stored IP prefixes on sessions and audit events are IPv4 /24 and IPv6 /48.
- Open registration disabled by default; `ALLOW_REGISTRATION` env flag creates a new user + new empty workspace.
- Audit events: login success/failure, logout, password change.
- CLI: `assetboard create-admin`, `assetboard reset-password <email>` (sets a new password interactively or via `--password-stdin` and revokes all sessions; the one-time-link variant is deferred to F1.5). `create-admin` is a break-glass command: it also works when users exist, never modifies an existing user (duplicate email is an error), creates an owned workspace unless `--no-workspace`, and is audited with the CLI as actor. Both respect `AUTO_MIGRATE` (off + schema not at head = non-zero exit, nothing changed).
- Setup gate: while there are zero users, GET /setup returns 200 with a form that includes a "setup token" field. The token is ASSETBOARD_SETUP_TOKEN if set, otherwise generated per process start and logged once at WARNING. POST /setup compares it with secrets.compare_digest, throttled; it creates the admin, workspace and owner membership in ONE transaction. Two concurrent POSTs: exactly one creates the admin and the other gets a deterministic "setup already completed" response (test). /setup returns 404 once any user exists.
- The zero-user redirect to /setup exempts /healthz, /readyz, /static/* and /setup itself (test). Unauthenticated users otherwise go to /login; `next` accepts only relative paths (open-redirect test).
- Emails are stripped and lowercased on write, with a unique index on the normalized value. Setup-form lists come from existing dependencies: currencies via Babel/minor_unit_for, time zones via zoneinfo, countries via Babel; no new dependency for them.
- Sessions: rotate the session id on login; cookie is `__Host-`-prefixed when BASE_URL is https; `last_seen_at` is updated at most every 5 minutes (avoid a write per request on SQLite); expiry uses the injected Clock.
- Throttle counters are stored in the database, keyed by account and by request.client.host (the real client IP only when ASSETBOARD_TRUSTED_PROXIES is set correctly behind a proxy; document this). Unknown users go through a dummy argon2 verify (timing test).
- argon2-cffi is a new runtime dependency: justify it in the PR (purpose, license, maintenance). The common-password list records its source and license in a NOTICE file (verify the license; do not assume).
- CLI (assetboard create-admin, reset-password) is non-interactive-capable (password via --password-stdin or prompt) and needs no setup token.
- scripts/smoke_container.sh and the compose CI step are updated for the new first-run behaviour: assert / redirects to /setup, /setup is 200, then create an admin via `docker exec ... assetboard create-admin`, then /setup is 404 and / redirects to /login. /healthz and /readyz stay green throughout.
- Documentation: remove or reword every "no authentication until M1" statement (README, SECURITY.md, docs/deployment.md, compose comments); add the new settings (ALLOW_REGISTRATION, session timeouts, SETUP_TOKEN, throttle limits) to the docs/deployment.md table; add ADR-0012 (local auth, server-side sessions, argon2id, setup token) and its index entry. All new UI strings exist in en and pl.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.1 from docs/specs/M1-identity-and-workspaces.md. In your plan, propose splitting it into two PRs:
(A) backend: User/Workspace/Membership/UserSession/AuditEvent models + migration, argon2id hashing, session service, throttle service, audit events, CLI create-admin/reset-password; no routes.
(B) web: setup-token-gated /setup wizard, login/logout/change-password, CSRF on all forms (must work with HTMX and the strict CSP, no inline scripts), the zero-user redirect middleware with the exemptions in the acceptance list, templates, en/pl translations, smoke-test and compose-CI updates, docs and ADR-0012.
Follow every bullet in the F1.1 acceptance list, including the extra bullets (setup token, concurrency, exemptions, cookie prefix, throttling, dummy verify, dependency justification, smoke test, docs).
Do not implement OIDC or invitations yet.
In the plan, list each acceptance criterion with the test that will prove it, and list every security assumption and open question. Do not write code until I say go.
```

---

## F1.2 Workspace context, roles and tenant isolation

**Story:** All data access is automatically scoped to my current workspace and my role.

**Acceptance**
- `Context(user, workspace, role, clock)` built per request from session + selected workspace (stored in session; workspace switcher in the top bar if the user has >1).
- `ScopedRepository[T]` base: every query adds `workspace_id = ctx.workspace.id`; inserts set it; there is no public method that bypasses scoping.
- `require_role("editor")`-style guard used in services; roles `owner > editor > viewer`.
- **Tenant isolation test:** a parametrized test discovers every mapped class with `workspace_id` and, using two workspaces, verifies that user B can never read, update or delete user A's rows through the repository API. Adding a new workspace-owned model without a repository fails this test (by design).
- Workspace settings page (owner): name, base currency, timezone, country, default locale. Members page (owner): list, change role, remove (cannot remove the last owner).
- Invitations: owner creates an invite link (role, expiry, email lock); invitee registers or logs in and accepts; token stored hashed, single use. (Changed in pre-release A: the email lock was optional except for owner invitations; it is now required for every role.)

**Delivery and decisions (2026-10-12)**
- Split into two PRs. **Part A:** Context, selected workspace in the session, role guards,
  `ScopedRepository`, the tenant-isolation test and `docs/architecture.md`. **Part B:**
  workspace switcher, workspace settings, members page and invitations.
- The selected workspace is `user_sessions.current_workspace_id`, NULL when a session starts.
  When it is NULL or no longer a membership, the Context uses the oldest membership (by
  `created_at`, then `id`) without storing it. Only `select_workspace`, called by the switcher,
  writes it.
- A signed-in user with no membership gets a 403 "No workspace" page on workspace pages.
  Account pages and sign-out keep working.
- A row of another workspace is a 404 with no workspace hint, the same as a missing row.
- Audit events are append-only, so their repository has no update or delete. Instance-level
  events (`workspace_id` NULL) are read only by the instance administrator's audit view (F1.5).
- Foreign keys between workspace-owned tables must include `workspace_id`.
- The isolation test also includes a static scan for workspace-owned models used outside
  repositories. See ADR-0003 (amendment 2026-10-12).
- Part B is split again. **B1:** switcher, workspace settings, members page and the last-owner
  rule. **B2:** invitations.
- The switcher is a form in the page header, shown only on workspace pages (those with a
  Context) when the user has more than one membership. Account, error, "No workspace" and
  signed-out pages render without workspace parts.
- Settings, members, role changes and removal are owners-only. Every write re-reads the
  actor's membership inside its `BEGIN IMMEDIATE` transaction (the request's Context can be
  stale) and checks the last-owner rule there. Owners cannot remove themselves (leaving a
  workspace is a later feature). Self-demotion works only when another owner exists.
- The workspace "default locale" is labelled "Formatting default": it does not change the
  interface language, which follows the user and the browser.
- Changing the base currency is allowed for now; M2 must block or convert once amounts exist.
- Removing a member does not revoke their sessions: the Context checks membership on every
  request.
- Backlog: workspace deletion, ownership transfer, leaving a workspace, and F1.6 TOTP second factor; revisit after 1.0 ([post-1.0 backlog](backlog-post-1.0.md)); until then use OIDC or proxy auth with an IdP that supports MFA.
- B2 (invitations), see ADR-0013:
  - Tokens are 256-bit, stored as SHA-256, single use; expiry is 1, 7 (default) or 30 days;
    at most 50 pending invitations per workspace.
  - Invitation sign-up works even when open registration is off and never creates an own
    workspace.
  - Every invitation requires an email lock (owner decision 2026-10-18, pre-release A; before,
    only owner invitations did). Invitations created earlier without one stay valid for any
    account until they expire, are used or are revoked; the members page marks them "Anyone
    with the link". The locked address is never displayed or pre-filled on the invitee side
    (the owner sees it in the pending list). A wrong email is counted per invitation
    (`failed_email_attempts`) and the invitation is revoked after 5. The lock is not proof of
    ownership.
  - An existing member is told so; the role is unchanged and the invitation not consumed.
  - Invalid links (expired, revoked, used, unknown or malformed) all look the same.
  - The token is in the URL path. The request log redacts it, uvicorn's access log is off, and
    invite, sign-in and registration pages send `Referrer-Policy: same-origin`, and every page
    sends `Cache-Control: no-store`.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.2 from docs/specs/M1-identity-and-workspaces.md: Context object, ScopedRepository base, role guards,
workspace switcher, workspace settings, members management, invitation links (hashed single-use tokens).
The key deliverable is the automated tenant-isolation test that introspects all mapped classes having workspace_id.
Also add a short docs/architecture.md section "How to add a new workspace-owned model" describing the required repository + test.
```

---

## F1.3 OIDC single sign-on

**Story:** I can sign in with my Authentik/Keycloak/Authelia/Google account.

**Acceptance**
- Generic OIDC via Authlib with discovery (`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_PROVIDER_NAME` for the button label); Authorization Code flow with **PKCE**; state + nonce validated.
- Account mapping: match existing `ExternalIdentity(provider, subject)`; otherwise, link to an existing user by email **only if** `OIDC_LINK_BY_EMAIL=true` **and** the IdP says `email_verified=true`; otherwise create a user if `OIDC_AUTO_CREATE=true`, else show "no account" page.
- Optional `OIDC_ALLOWED_DOMAINS`; optional group→role mapping via `OIDC_GROUPS_CLAIM` and `OIDC_ADMIN_GROUP`.
- `OIDC_ONLY=true` hides local login; a break-glass flag `ALLOW_LOCAL_ADMIN_LOGIN=true` keeps `/login/local` for instance admins.
- Optional RP-initiated logout.
- Tests use a fake in-process IdP (no network): happy path, wrong nonce/state, unverified email, domain restriction, account linking rules.
- Docs: `docs/auth/oidc.md` with setup notes for Authentik, Keycloak, Authelia (generic checklist; do not claim version-specific details you did not verify).

**Delivery and decisions (2026-10-12)**, see ADR-0014:
- Split into two PRs. **Part A:** settings, `ExternalIdentity` and `user_sessions.auth_method`,
  the OIDC client, the signed flow and pending-link payloads, account mapping, password
  confirmation, the fake provider and service tests. **Part B:** routes, templates,
  `OIDC_ONLY`/break-glass, RP logout, translations, the browser test and `docs/auth/oidc.md`.
- Linking by email needs the flag and the existing account's password, entered once (login
  throttle, dummy verify, generic error), and revokes that account's other sessions. Accounts without a
  password are never linked by email; the visitor is told the account exists.
- Apart from signing in with an already linked identity, OIDC needs a verified email, checked
  before any local account is looked at (an unverified claim cannot probe for accounts).
  Creating an account (auto-create or invitation) uses the registration throttle. An invitation creates the account even without
  `OIDC_AUTO_CREATE`. The domain allow-list needs a verified email and is checked at every
  sign-in.
- `email_verified` counts only as `true` or `"true"`; userinfo claims are used only when its
  `sub` equals the ID token's.
- `OIDC_ADMIN_GROUP` maps one group to instance administrator (grant and revoke); a missing or
  malformed claim changes nothing; the last active administrator is never demoted. Workspace
  roles are not mapped.
- Flow state is a signed cookie with `iat`/`exp` checked by the server (10 minutes), not a
  table. The provider is reached with the standard library (no new HTTP client).
- `BASE_URL` must be set and `https://` (as must the issuer and every discovered endpoint)
  unless `ENV=dev`. `OIDC_ONLY` refuses to start with `ALLOW_REGISTRATION`.
- `/auth/oidc/*` sends `Referrer-Policy: no-referrer` and `Cache-Control: no-store` and has no
  forms; the password confirmation page is under `/login/`.
- Part B: the callback answers every outcome with a 303 to a URL without a query; failures go
  to `/auth/oidc/failed/<reason>` (fixed messages; `busy` and `unavailable` answer 503 with
  `Retry-After`). A locked database at the callback therefore shows the busy page after the
  redirect. `next` is at most 512 ASCII characters. `/login/local` (break-glass) treats a
  non-administrator's correct password exactly like a wrong one. RP logout ends the local session,
  then shows a page that continues to the provider (meta refresh and a link).
- Part B review: failures before the provider accepted the code (token endpoint errors
  included) are logged, not audited (anyone can craft them); each flow state is accepted once (in-memory, per process, 10 minutes); the
  "account exists" page no longer mentions the profile until F1.5 adds linking there.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.3 from docs/specs/M1-identity-and-workspaces.md: generic OIDC login with Authlib, PKCE, state/nonce,
account linking rules (verified email only, behind explicit flags), auto-create, domain allow-list, OIDC_ONLY mode with break-glass,
optional group->role mapping. Build a fake in-process IdP for tests; no network in tests.
Security is the priority: list every account-takeover scenario you considered in the PR description.
Write docs/auth/oidc.md.
```

---

## F1.4 Reverse-proxy header authentication (optional)

**Story:** If I already use forward-auth (Authelia/Authentik proxy), the app trusts the proxy's identity headers.

**Acceptance**
- Disabled by default. Enabled with `PROXY_AUTH_ENABLED=true`, `PROXY_AUTH_USER_HEADER`, `PROXY_AUTH_EMAIL_HEADER`, and **`TRUSTED_PROXIES` required** (CIDR list).
- Headers are honoured only if the TCP peer is inside `TRUSTED_PROXIES`; otherwise they are ignored and logged at WARNING.
- Same account-mapping rules as OIDC (`PROXY_AUTH_AUTO_CREATE`).
- Startup refuses to run with proxy auth enabled and empty `TRUSTED_PROXIES`.
- Tests: spoofed headers from untrusted peer are ignored; trusted peer works.

**Implementation notes (agreed in the F1.4 plan, [ADR-0015](../adr/0015-proxy-header-authentication.md))**
- Extra settings: `PROXY_AUTH_NAME_HEADER` (optional, names new accounts) and
  `PROXY_AUTH_LINK_BY_EMAIL` (default false). `PROXY_AUTH_AUTO_CREATE` defaults to false. No
  domain allow-list and no group mapping.
- The trusted peer is the TCP peer recorded by `PeerMiddleware` before forwarded headers apply.
  Startup also refuses `TRUSTED_PROXIES` entries wider than IPv4 /24 or IPv6 /64 or covering
  IPv4-mapped IPv6 addresses, and warns about every entry wider than one address.
- Delivered in two parts: A (settings and validation, peer capture, shared account mapping,
  migration 0006) and B (sign-in routes and session binding, logout, templates, user docs).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.4 from docs/specs/M1-identity-and-workspaces.md: optional reverse-proxy header authentication,
disabled by default, honoured only from TRUSTED_PROXIES CIDRs, startup validation, tests for spoofed headers.
Add a prominent warning section to docs/auth/proxy-auth.md.
```

---

## F1.5 Profile, instance admin and audit log view

**Acceptance**
- Profile page: display name, locale, theme, password (if local), linked identities (unlink if another login method remains).
  Locale: "Browser language" (stored as NULL, the default for every account) follows
  Accept-Language; a saved language wins over it. Sensitive actions (link, unlink, sign out
  everywhere) are confirmed with the password, or without one by a sign-in from the last
  10 minutes (ADR-0012 amendment).
- Instance admin page: users list, activate/deactivate, promote/demote instance admin, create user with temporary password.
  Every action is confirmed by the acting administrator (password, or a fresh external
  sign-in). The temporary password is generated and shown once; the account must change it
  before anything else and starts without a workspace (it sees the "no workspace" page until
  it is invited). Account creation is not offered under `OIDC_ONLY`.
- Audit log page (owner/instance admin): filterable list of `AuditEvent`s; auth and membership events appear.
  Two scopes: the owner sees the current workspace's events; an instance administrator sees
  instance-level events (`workspace_id IS NULL`) only, never another workspace's. Keyset
  pages; data rendered only through per-action key allowlists; IP prefixes and the affected
  account only in the instance scope.
- Deactivated users cannot log in and their sessions are revoked.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.5 from docs/specs/M1-identity-and-workspaces.md: profile page, linked identities management,
instance-admin user management, audit log viewer with filters and pagination. Deactivation must revoke sessions.
Tests for authorization (viewer/editor cannot reach admin pages).
```

---
