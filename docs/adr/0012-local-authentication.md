# ADR-0012: Local authentication, server-side sessions and first-run setup

**Status:** Accepted
**Date:** 2026-10-11

## Context

AssetBoard holds personal financial data and is self-hosted, often on a home network behind a
reverse proxy, in a single process with SQLite. M1 needs sign-in before any financial feature
exists. The first administrator must be created without a default password, and the design must
hold up against password guessing, cross-site request forgery and session theft without relying
on infrastructure a homelab may not have.

## Decision

- **Local accounts with Argon2id.** Passwords are hashed with argon2-cffi's default parameters
  (minimum length 8, a bundled common-password list, no composition rules; see the amendment below). Hashing runs in a
  dedicated thread pool behind a concurrency limit (`ASSETBOARD_AUTH_MAX_CONCURRENT_HASHES`, at most
  16 waiting); a saturated limit answers 503 with `Retry-After` instead of queueing without bound.
  A dummy hash computed at startup is verified for unknown users so they cost the same as known
  ones. Rehashing after a parameter change is best effort and never happens inside a write
  transaction.
- **Server-side sessions.** The cookie holds a random 256-bit identifier; the database stores
  only its SHA-256. Cookies are `HttpOnly`, `SameSite=Lax`, `Path=/`, without `Domain`, and
  `Secure` with the `__Host-` prefix when `BASE_URL` is https. Max-Age follows the absolute
  lifetime (60 days); the idle timeout (14 days) is enforced on the server. Sign-in rotates the
  session; a password change signs out other sessions. Recording activity is best effort and
  never fails a request.
- **First-run setup token.** While no user exists, every page redirects to `/setup`, which needs a
  setup token: `ASSETBOARD_SETUP_TOKEN` if set (never logged), otherwise a token generated at each
  process start and logged once at `WARNING`. Setup is serialised by a single `setup_state` row;
  exactly one concurrent request wins and the others get 409. `assetboard create-admin` is a
  break-glass alternative that needs no token.
- **CSRF.** Every state-changing route has a router-level dependency that checks a token from the
  `X-CSRF-Token` header (HTMX) or the `csrf_token` form field. Signed-in pages use an HMAC of the
  session hash; signed-out forms use a signed double-submit token bound to a random cookie, valid
  for 12 hours. Both are derived from a purpose-specific subkey of `SECRET_KEY` and are masked with
  a fresh pad in every response (BREACH). An Origin check runs first: `Sec-Fetch-Site:
  cross-site` is refused, and an `Origin`, when present, must be the `BASE_URL` origin or the
  origin the request was addressed to.
- **Throttling.** Failures are counted per account (5 free) and per client address (20 free per
  hour), with exponential backoff capped at 15 minutes; registration has its own per-address
  limit. Attempts are reserved in a short `BEGIN IMMEDIATE` transaction before the password is
  checked (ADR-0007), so parallel requests cannot all slip through. The client address is only
  `request.client.host`; Uvicorn rewrites it from forwarded headers only for
  `ASSETBOARD_TRUSTED_PROXIES` (empty trusts no proxy).
- **Schema state.** The schema is checked once at startup. A database behind the code (with
  automatic migration off) or ahead of it makes every page answer 503 and `/readyz` fail, with no
  setup token.

### Middleware order

Outermost first: `SecurityHeaders` → `Locale` → `RequestId` → `OriginCheck` → `AccessGate` →
routes, where the CSRF dependency runs. The access gate (schema state, then setup, then sign-in)
answers before any route. So a POST without a CSRF token gets 503 while the schema is not current
and a 303 to `/setup` while setup is open, and a cross-site POST is refused (403) before either.
Neither middleware reads a request body; forms are parsed once, by FastAPI.

## Consequences

- One process only: the setup token, the hashing limit and the "setup closed" cache live in
  memory, so the app must run as a single Uvicorn worker.
- No user enumeration through sign-in: unknown email, wrong password and throttled attempts get
  the same response.
- A failed sign-in returns 401 without `WWW-Authenticate`: no HTTP authentication scheme is
  offered (the form is the only way in), and the 401 lets log-based tools such as fail2ban or
  proxy rules count failures without parsing the page.
- Accepted risks:
  - **Account lockout** by an attacker is bounded by the 15-minute backoff cap.
  - **Registration discloses existing emails** ("cannot be registered"); registration is off by
    default and limited per address. There is no email verification yet.
  - **The generated setup token is in the logs**; anyone who can read them can complete setup
    until it is done. Set `ASSETBOARD_SETUP_TOKEN` to avoid this.
  - **Single worker only**, as above.
  - **The common-password list does most of the work at the minimum length**: 2,087 of its 10,000
    entries have 8 or more characters (51 had 10 or more under the original minimum).
  - **Shared address bucket**: without a correct `ASSETBOARD_TRUSTED_PROXIES`, clients behind a
    proxy or Docker port publishing share one per-address budget.
  - **Throttled responses are fast**, which reveals that throttling applies (not whether the
    account exists).
  - **Cookie injection over plain HTTP.** On an `http://` `BASE_URL`, cookies have no `__Host-`
    protection, so a network attacker (or a sibling host on the same domain) can plant an
    anonymous CSRF cookie and forge a matching token, which fixes the anonymous token. This only
    reaches signed-out forms: login CSRF (signing a victim into the attacker's account) and
    registration. Signed-in forms use session-bound tokens. Use HTTPS to remove this risk.
  - **Blocked setup attempts take the write lock.** `POST /setup` checks its throttle inside a
    short `BEGIN IMMEDIATE`, so even a blocked attempt briefly takes SQLite's write lock (no
    read-only pre-check as for login). Setup is only reachable until the first administrator
    exists, and the token check is cheap, so this is accepted.
  - **Host is not validated** (DNS rebinding). The Origin check accepts the origin a request was
    addressed to, so a rebinding page could post same-origin forms; it would still need a valid
    CSRF token, and host-scoped cookies (no `Domain`, `__Host-` on https) are never sent to the
    attacker's name, which limits the impact to signed-out forms.

## Amendment (2026-10-12): OIDC sign-in (F1.3)

ADR-0014 adds OIDC sign-in. For local authentication this means:

- **Users without a password.** Accounts created through OIDC have `password_hash` NULL. Sign-in
  treats them like unknown users (dummy verify, the same generic error), so no password works,
  not even an empty one. Changing the password reports that there is none, without counting an
  attempt. `assetboard reset-password` can still set one (break-glass).
- **Local sign-in can be turned off.** `ASSETBOARD_OIDC_ONLY` hides password sign-in and
  registration; `ASSETBOARD_ALLOW_LOCAL_ADMIN_LOGIN` keeps `/login/local` for instance
  administrators.
- **Password confirmation.** Linking an OIDC identity to an account that has a password needs
  that password once. The check reuses login's per-account and per-address throttle and dummy
  verify; F1.5 reuses it for sensitive profile actions.
- **Sessions record how they started** (`auth_method`: `password` or `oidc`).

## Amendment (2026-10-12): proxy header authentication (F1.4)

ADR-0015 adds reverse-proxy header authentication. For local authentication this means:

- **Forwarded headers are applied by `PeerMiddleware`.** `assetboard serve` runs uvicorn with
  `proxy_headers=False`; `PeerMiddleware` records the TCP peer, then applies forwarded headers
  from `ASSETBOARD_TRUSTED_PROXIES` with uvicorn's own `ProxyHeadersMiddleware`. The client
  address used for throttling (`request.client.host`) is unchanged.
- **Sessions record how they started** (`auth_method`: `password`, `oidc` or `proxy`).

## Amendment (2026-10-13): profile and sensitive actions (F1.5)

- **One confirmation entry point.** Linking or unlinking a sign-in method and signing out other
  devices are confirmed through `services.profile.confirm_sensitive`. An account with a password
  always confirms with it, through the existing `confirm_password_with_factory` primitive
  (login's throttle, dummy verify, audited failures), unchanged.
- **Accounts without a password** (created through OIDC or the proxy) confirm with a fresh
  external session instead: the current session started through OIDC or the proxy at most
  10 minutes ago (`UserSession.created_at`, not its last activity). **This is weaker than a
  password: whoever holds the session cookie in those 10 minutes after the sign-in can confirm.**
  Outside that window the action is refused ("sign in again") and nothing is written. For an
  OIDC link the window applies to **starting** it: the provider round trip may then finish up to
  10 minutes later, the lifetime of the signed flow cookie. Successful
  actions record how they were confirmed: `{"via": "profile", "confirmed": "password"}` or
  `"fresh_session"`.
- **Never without a way to sign in.** Unlinking checks, in the same `BEGIN IMMEDIATE`
  transaction as the delete, that a usable method remains: a password that today's settings
  allow (not under `OIDC_ONLY`, unless the user is an administrator and
  `ALLOW_LOCAL_ADMIN_LOGIN` is on), an identity of the configured OIDC issuer, or a proxy
  identity while proxy authentication is on. Unlinking an OIDC identity ends the user's other
  OIDC sessions; proxy sessions end through the proxy binding (ADR-0015).
- **Language.** `users.locale` is nullable: NULL follows `Accept-Language`, a saved language
  wins over it (there is no per-device language cookie). New accounts start with NULL.
- **Migrations on SQLite** run with foreign-key enforcement off and check `PRAGMA
  foreign_key_check` before committing, so rebuilding a referenced table (batch mode) cannot
  cascade-delete the rows that point at it.
- **Administrator actions** (activate, deactivate, promote, demote, create an account) go
  through the same confirmation entry point as profile actions. The change runs in the
  confirming `BEGIN IMMEDIATE` transaction, which checks again that the actor is an active
  instance administrator; self-deactivation, self-demotion and removing the last active
  administrator are refused there, so two administrators acting on each other at once keep
  one. Deactivation ends every session of the account (all sign-in methods).
- **Temporary passwords.** An administrator-created account gets a generated password
  (`secrets.token_urlsafe`, 16 characters, checked against the password policy) shown once in
  the response and never stored in readable form, logged or audited. `users.must_change_password`
  (migration 0008) then limits that account's password sessions to the password page and
  signing out until the password changes; OIDC and proxy sessions are not limited.

## Amendment (2026-10-14): minimum password length 8

The minimum length drops from 10 to 8 characters, the floor NIST SP 800-63B sets for
user-chosen passwords that are checked against a list of commonly used ones. The bundled
common-password list, the identity check (not the email name or display name) and the
throttles are unchanged. Existing passwords are unaffected; the rule applies when a password is
set or changed. Temporary passwords are still 16 characters.
