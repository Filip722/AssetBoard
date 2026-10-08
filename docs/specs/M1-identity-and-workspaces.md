# M1 — Identity and workspaces

**Goal:** secure multi-user foundation before any financial feature exists, so tenant scoping is never retrofitted.

**Exit criteria:** first-run setup creates an admin + workspace; users can log in locally or via OIDC; roles are enforced; an automated test proves no cross-workspace data access for every workspace-owned model.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `User(id, email unique ci, display_name, password_hash?, locale, theme, is_active, is_instance_admin, created_at, last_login_at)`
- `ExternalIdentity(user_id, provider, subject, email_at_link, created_at)`; unique `(provider, subject)`
- `UserSession(id_hash, user_id, created_at, last_seen_at, expires_at, user_agent, ip_prefix)`
- `Workspace(id, name, base_currency, locale, timezone, country_code, created_at)`
- `Membership(workspace_id, user_id, role)`; unique `(workspace_id, user_id)`
- `Invitation(id, workspace_id, token_hash, role, email?, expires_at, used_at?, created_by)`
- `AuditEvent(...)` as defined in the overview §3.9

---

## F1.1 Local accounts, sessions and first-run setup

**Story:** On first start I create the admin account and my workspace; afterwards I log in with email + password.

**Acceptance**
- With zero users, every route redirects to `/setup` (create admin: email, display name, password; workspace: name, base currency, country, timezone, locale). `/setup` returns 404 once a user exists.
- Passwords: argon2id; minimum length 10, no composition rules; check against a small bundled common-password list.
- Sessions are **server-side** (`UserSession`), cookie holds an opaque random id (stored hashed), `HttpOnly`, `SameSite=Lax`, `Secure` when `BASE_URL` is https; idle timeout 14 days, absolute 60 days (configurable); "log out other devices".
- Login/logout, change password (invalidates other sessions), CSRF on all forms.
- Login throttling per IP and per account (e.g., 5 failures → exponential backoff), generic error messages (no user enumeration), constant-time-ish comparison path for unknown users.
- Open registration disabled by default; `ALLOW_REGISTRATION` env flag creates a new user + new empty workspace.
- Audit events: login success/failure, logout, password change.
- CLI: `AssetBoard create-admin`, `AssetBoard reset-password <email>` (prints one-time link or sets a new password interactively).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.1 from docs/specs/M1-identity-and-workspaces.md: User/Workspace/Membership/UserSession/AuditEvent models + migration,
first-run /setup wizard, argon2id password hashing, server-side sessions (opaque id stored hashed), login/logout/change-password,
CSRF on forms, login throttling, CLI create-admin and reset-password.
Include tests for: setup only works when there are no users, throttling, session expiry (use the fixed Clock), no user enumeration.
Do not implement OIDC or invitations yet.
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
- Invitations: owner creates an invite link (role, expiry, optional email lock); invitee registers or logs in and accepts; token stored hashed, single use.

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
- Instance admin page: users list, activate/deactivate, promote/demote instance admin, create user with temporary password.
- Audit log page (owner/instance admin): filterable list of `AuditEvent`s; auth and membership events appear.
- Deactivated users cannot log in and their sessions are revoked.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.5 from docs/specs/M1-identity-and-workspaces.md: profile page, linked identities management,
instance-admin user management, audit log viewer with filters and pagination. Deactivation must revoke sessions.
Tests for authorization (viewer/editor cannot reach admin pages).
```

---

## F1.6 (stretch) TOTP two-factor authentication

**Acceptance:** optional per user; TOTP via `pyotp`; 10 single-use recovery codes (hashed); enforcement is skipped for OIDC logins (IdP's responsibility); throttling on code entry; owner can require 2FA for local logins.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.6 (stretch) from docs/specs/M1-identity-and-workspaces.md: TOTP enrollment (QR + manual key), verification at login,
hashed recovery codes, throttling, disable flow requiring password. Tests with a fixed Clock for TOTP windows.
```
