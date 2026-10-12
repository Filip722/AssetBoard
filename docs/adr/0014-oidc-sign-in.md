# ADR-0014: OIDC sign-in

**Status:** Accepted
**Date:** 2026-10-12

## Context

Homelab users often run an identity provider (Authentik, Keycloak, Authelia) or use Google, and
want one sign-in for their services (F1.3). An OIDC sign-in hands the app an identity it did not
check itself, so the dangerous part is mapping that identity to an account: a careless email
match lets whoever controls an email claim at the provider take over a local account. The app
is a single process with SQLite, runs behind a strict CSP (`form-action 'self'`) and must not
need the provider at startup.

## Decision

- **Protocol.** Authorization Code flow with PKCE (S256), `state` and `nonce`, against one
  provider found by discovery (`<issuer>/.well-known/openid-configuration`).
  - The discovery document's `issuer` must equal `ASSETBOARD_OIDC_ISSUER` exactly (mix-up
    protection), and every endpoint must be `https://` (except with `ENV=dev`).
  - Discovery is fetched on first use and cached for an hour; a failure is cached for
    30 seconds. Keys are cached for an hour and fetched again once when a token names an
    unknown key, at most once a minute.
  - The ID token must be signed with an asymmetric algorithm from our own allowlist (RSA, RSA-PSS,
    ECDSA, EdDSA; never `none` or HMAC). We check `iss`, `aud`, `azp` (when there are several
    audiences or it is present), `exp` and `iat` with a 60-second skew, `nonce` and `sub`.
  - The authorization response's `iss` parameter is checked when present, and required when
    the provider advertises it (RFC 9207). Provider errors get fixed messages; their
    `error_description` is only logged.
  - Claims missing from the ID token come from userinfo, only if its `sub` equals the ID
    token's. Email and `email_verified` always come from the same document. `email_verified`
    counts only as boolean `true` or the string `"true"`.
  - The redirect URI is always `<BASE_URL>/auth/oidc/callback`, never derived from request
    headers, so `BASE_URL` must be set explicitly (and `https://` outside dev).
- **HTTP without a new client library.** The four provider calls (discovery, keys, token,
  userinfo) use the standard library behind a small transport interface: 10-second timeouts, no
  redirects, responses capped at 1 MiB, and the system trust store (`SSL_CERT_FILE` works for a
  private CA). Tests replace the transport with an in-process fake provider. Authlib is used
  only for its protocol helpers (authorization URL, token request body, S256 challenge); joserfc
  verifies signatures. The reason is that we add no runtime HTTP client for four requests. It
  also avoids a choice in flux: Authlib 1.8.0's changelog (2026-08-30) moved its httpx
  integration to `httpx2` and warns when it falls back to `httpx`, whose latest stable release
  is 0.28.1 (December 2024; 1.0 is in development releases).
- **Flow state in a signed cookie.** State, nonce, the PKCE verifier and `next` travel in an
  HttpOnly, `SameSite=Lax` cookie (`__Host-` on https) signed with a subkey of `SECRET_KEY`,
  with `iat` and `exp` (10 minutes) inside the signed payload and checked by the server. No
  table is needed and an invitation token in `next` never reaches the database. The callback
  deletes the cookie, and the flow's state is recorded as used right after the cookie is read,
  before any provider call or database write, so a replayed cookie and callback URL go to
  "start over". That record lives in memory (only state hashes, for 10 minutes, at most 10,000
  entries with the oldest evicted first): it is per process and lost on restart, which the
  single worker (ADR-0012) makes acceptable; the provider's single-use code backs it up.
  Because the cookie binds the callback to the browser that started the flow, a callback URL
  planted by someone else fails (login CSRF). Starting a second flow in another tab replaces
  the cookie, and the first tab's callback then asks the user to start over.
- **Account mapping, in a fixed order.**
  1. The domain allow-list, when set, needs a verified email in a listed domain (exact match),
     checked at every sign-in.
  2. A linked `ExternalIdentity(issuer, subject)` signs that user in, unless inactive.

     From here on the email must be verified. This is checked before any local account is
     looked at, and the answer ("email not verified") is the same whether or not the address is
     registered, so an unverified claim cannot probe for accounts.
  3. If an account with the same email exists, it is linked only with
     `ASSETBOARD_OIDC_LINK_BY_EMAIL` **and that account's password entered once** on a
     confirmation page (login's throttle and dummy verify, a generic error).
     Linking revokes the account's other sessions. Accounts without a password are never
     linked by email. Every other case gets "an account with this email exists; sign in with
     your password, then link from your profile" (linking from the profile comes in F1.5).
  4. An invitation in `next` creates an account that joins only that workspace, even without
     auto-create, under the invitation's email lock (ADR-0013 amendment).
  5. `ASSETBOARD_OIDC_AUTO_CREATE` creates an account with its own workspace.
  6. Otherwise the visitor sees "no account".

  Creating an account (4, 5) counts against the per-address registration limit. Accounts
  created through OIDC have no password. Provider display names are cleaned (control and
  format characters removed, whitespace collapsed, at most 120 characters, `User` if nothing
  is left), and provider text in logs never contains a line break. Refusals after the
  provider accepted the code (ID token, userinfo, account mapping) are audited as
  `auth.oidc_failure` with a reason code, never with tokens, codes or claims. Refusals before
  the provider accepted the code (token endpoint errors included: missing or replayed flow,
  state, `iss`, provider `error=` responses, `invalid_grant` for a junk code) can be triggered
  by anyone with a crafted URL, so they go to the application log only, not the append-only
  audit table.
- **Admin group.** With `ASSETBOARD_OIDC_ADMIN_GROUP`, each OIDC sign-in sets
  `is_instance_admin` from the groups claim. A present claim is authoritative, even when empty;
  an absent or malformed claim changes nothing and logs a WARNING. The last active instance
  administrator is never demoted. Workspace roles are not mapped; they come from invitations
  and the members page.
- **Sessions.** Every OIDC sign-in creates a new session; a session the browser already had is
  ended, whoever it belonged to. Sessions record how they started (`auth_method`), so
  RP-initiated logout applies only to OIDC sessions.
- **Local sign-in.** `ASSETBOARD_OIDC_ONLY` hides password sign-in and registration; it
  refuses to start together with `ALLOW_REGISTRATION`. `ASSETBOARD_ALLOW_LOCAL_ADMIN_LOGIN`
  keeps `/login/local` for instance administrators as a break-glass route, with the same
  throttle and generic error as `/login`.
- **RP-initiated logout** (`ASSETBOARD_OIDC_RP_LOGOUT`). Sign-out ends the local session, then
  shows a page that continues to the provider's `end_session_endpoint` with `client_id` and
  `post_logout_redirect_uri=<BASE_URL>/login`. A page, not a redirect: Chromium applies
  `form-action 'self'` to redirects that follow a form POST. No ID token is stored, so no
  `id_token_hint` is sent.
- **Leaks.** `/auth/oidc/*` responses send `Referrer-Policy: no-referrer` and `Cache-Control:
  no-store`; they contain no forms (under `no-referrer` browsers send `Origin: null` on POSTs:
  see ADR-0013 for why pages with forms use `same-origin`, and ADR-0012 for the Origin check
  that refuses `null`). The password confirmation form lives under `/login/` with
  `same-origin`. The callback never renders a page: every outcome, errors included, is a 303
  to a URL without a query (`next`, `/login/link` or `/auth/oidc/failed/<reason>`, which shows
  a fixed message per reason), and the request log never records query strings.
- **Cookie size.** `next` for OIDC is a local path of at most 512 ASCII characters (anything
  else becomes `/`), the issuer is ASCII (at most 512 characters) and payloads are UTF-8, so the
  largest pending link (255-character subject, 320-character email) still fits in a cookie. A
  payload that did not fit would show the generic failure page, never an error.

## Consequences

- No new HTTP client: the provider calls are about 150 lines of standard-library code that we
  maintain ourselves.
- One provider per instance. A second provider would need a provider key in the settings and
  the flow cookie.
- Accepted risks:
  - **No back-channel logout.** Disabling a user at the provider does not end their sessions
    here; they last until the idle or absolute timeout, or until an administrator deactivates
    the account (F1.5).
  - **Changing the issuer URL orphans identities.** Identities are keyed by the issuer string.
    After a change, an account that has a password can be linked again (with
    `OIDC_LINK_BY_EMAIL` and that password). An account created through OIDC has no password,
    so it is never linked by email: an administrator first sets one with
    `assetboard reset-password <email>`, then the user links with it (or, from F1.5, signs in
    with it and links from the profile).
  - **Providers that leave `email_verified` false** (for example Keycloak users whose email was
    never verified) block account creation, linking by email, invitations and the domain
    allow-list for those users. Existing linked identities keep working.
  - **Linking trusts the provider's verified email.** A provider administrator who can mark
    any address as verified can start a link to any local account, but cannot finish it without
    that account's password.
  - **Reverse-proxy logs record the callback query**, which holds a one-time code that is
    useless without the browser's flow cookie and PKCE verifier.
  - **Provider-controlled display names** are stored as given (escaped on output).
