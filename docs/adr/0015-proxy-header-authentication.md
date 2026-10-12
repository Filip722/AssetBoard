# ADR-0015: Reverse-proxy header authentication

**Status:** Accepted
**Date:** 2026-10-12

## Context

Homelab users often put their services behind a forward-auth proxy (Authelia, Authentik's proxy
outpost, oauth2-proxy) that authenticates the person and passes their identity on in request
headers such as `Remote-User` and `Remote-Email` (F1.4). Those headers are ordinary request
headers: any client that can reach the app directly can send them. Header authentication is
therefore only as safe as two things: the app reading them only from the proxy, and the proxy
overwriting whatever the client sent.

Before F1.4, `assetboard serve` let uvicorn apply `X-Forwarded-For` and `X-Forwarded-Proto` from
`ASSETBOARD_TRUSTED_PROXIES` (ADR-0012). Uvicorn replaces `scope["client"]` with the forwarded
address before the app runs, so the TCP peer, which is the only address that says whether a
request came from the proxy, was lost.

## Decision

- **Off by default.** `ASSETBOARD_PROXY_AUTH_ENABLED=true` turns it on, with
  `PROXY_AUTH_USER_HEADER`, `PROXY_AUTH_EMAIL_HEADER` and an optional `PROXY_AUTH_NAME_HEADER`.
- **Only the TCP peer is trusted, and it is recorded before forwarded headers apply.**
  `assetboard serve` wraps the app in `PeerMiddleware` and runs uvicorn with
  `proxy_headers=False`. `PeerMiddleware` stores the peer under `scope["assetboard.peer"]`, then
  applies forwarded headers with uvicorn's own `ProxyHeadersMiddleware` and the same
  `TRUSTED_PROXIES` string, so `request.client`, `client_host` and the scheme behave exactly as
  before. Identity headers are read only when that recorded peer is inside `TRUSTED_PROXIES`.
  `X-Forwarded-For` never makes a peer trusted.
  - Comparison: the peer is parsed as an IP address; `::ffff:a.b.c.d` from dual-stack sockets
    is compared as IPv4 `a.b.c.d`; IPv4 peers match only IPv4 networks and IPv6 peers only IPv6
    networks. No peer, a Unix socket or anything that is not an IP address is never trusted,
    and neither is a peer with an IPv6 zone (`fe80::1%eth0`): the same link-local address can
    be a different host on each interface.
  - Without the recorded peer (the app was started some other way than `serve`), nothing is
    trusted and one ERROR per process says to use `assetboard serve`. `make dev` runs the same
    wiring through the `assetboard.cli:dev_app` factory (uvicorn `--reload` needs a factory).
- **Startup refuses unsafe settings** when proxy auth is on: a missing user or email header
  name; header names that repeat (compared case-insensitively) or that are not HTTP tokens or
  are reserved for something else (`Host`, `Cookie`, `Authorization`, `Origin`, `Forwarded`,
  `X-Forwarded-For`/`-Proto`/`-Host`/`-Port`, `X-Real-IP`, `Sec-*`, `HX-*` and similar); an
  empty `TRUSTED_PROXIES`; any entry wider than **IPv4 /24 or IPv6 /64** (which also rules out
  `0.0.0.0/0` and `::/0`); and IPv6 entries that cover IPv4-mapped addresses (`::ffff:0:0/96`),
  which would never match because such peers are compared as IPv4. Every accepted entry that is
  wider than one address is named in a startup WARNING.
- **What the headers are trusted for.** The user header is the subject, stored as
  `ExternalIdentity(provider="proxy", subject=…)`. The email header counts as a **verified**
  email (the proxy vouches for it); a missing or malformed email counts as unverified. The name
  header only names a new account. Groups are not read: proxy sign-in never changes instance
  administrator status. A header that occurs more than once is refused; an empty value counts
  as absent; values must be UTF-8 without control or format characters, line or paragraph
  separators, private-use or unassigned code points, and the subject at most 255 characters.
- **One account-mapping implementation.** OIDC and the proxy share `sign_in_external`; a
  `MappingPolicy` per provider carries what differs: `PROXY_AUTH_AUTO_CREATE` and
  `PROXY_AUTH_LINK_BY_EMAIL` (both off by default), no domain allow-list, no admin group,
  `auth_method="proxy"` and the audit action names (`auth.proxy_failure`,
  `auth.proxy_linked`). Everything else is the F1.3 order: linked identity (refused if
  deactivated), verified email required from there on, an existing account by email (link only
  with the flag, only to an account with a password, after that password), invitation in
  `next`, auto-create under the registration throttle, otherwise no account.
- **Sessions are bound to the proxy.** Sessions record `auth_method='proxy'` (migration 0006).
  A proxy session is honoured only while a trusted peer presents the subject whose
  `ExternalIdentity(provider="proxy")` belongs to the session's user. A different subject
  revokes the session and signs in again on GET, and counts as signed out for unsafe methods.
  An untrusted peer, a missing header or repeated or invalid identity headers count as signed
  out for that request, without revoking; so do all proxy sessions while proxy authentication
  is off (the rows stay). A subject whose `ExternalIdentity` row was deleted counts as a
  different subject. Password and OIDC sessions are never affected by proxy headers, so
  break-glass `/login/local` and OIDC keep working.
- **Sign-in route.** Signed-out GET and HEAD page loads (and htmx requests, via `HX-Redirect`) from
  a trusted peer with valid headers are sent to `/auth/proxy?next=…`; `/login` and invitation pages
  link there ("Continue as <user>", the escaped header value). A browser that already has a proxy
  session for the same subject is sent on to `next` unchanged; in every other case (no session, a
  password or OIDC session, another subject) the session is rotated. An untrusted peer, missing
  headers and repeated or invalid headers all get the same failure page, so the answer tells
  nothing about which check failed. `/login` and invitation pages can name the proxy's user, so
  they rely on `Cache-Control: no-store`, which every page sends. Every `/auth/proxy*` route
  answers 404 while proxy authentication is off, and sends no Referer (`next` can be
  `/invite/<token>`).
- **No redirect loop.** A new proxy session is first sent to `/auth/proxy/continue`, which
  goes on to `next` only if the session cookie came back. A browser that drops the cookie (an
  `https` `BASE_URL` opened over `http`, blocked cookies) gets one round of redirects and then
  a page that explains it, with no secret in any URL and no second cookie.
- **Password confirmation** (`/login/link`) is shared with OIDC and works with proxy
  authentication alone; its wording names no provider.
- **Logout** ends the session only; there is no proxy logout call. The proxy usually signs the
  browser straight back in on the next page, which the documentation states.
- **Spoofing attempts are logged, not audited.** Identity headers from an untrusted peer are
  ignored and logged at WARNING (header name and peer prefix, never the value), at most once
  per peer prefix per 10 minutes. Anyone can send such headers, so they write no audit rows.
  Refusals after a trusted peer presented an identity are audited as `auth.proxy_failure`
  once per SHA-256 of the subject and reason per 10 minutes, then logged only; repeated or
  invalid headers from the trusted proxy are logged at WARNING and audited once per reason per
  10 minutes. Both limits live in bounded in-memory maps per process (restarts reset them).
- **Unchanged protections.** CSRF tokens and the Origin check apply to proxy sessions exactly as
  to others. `OIDC_ONLY` and `ALLOW_LOCAL_ADMIN_LOGIN` keep their meaning; proxy sign-in uses
  no password, so the password throttles do not apply, while account creation uses the
  registration throttle and linking uses the password-confirmation throttle.
- **Delivery.** F1.4 part A implemented the settings and startup checks, `PeerMiddleware`, the
  peer and header parsing, the shared account mapping and migration 0006. Part B added the
  sign-in routes, session binding, logout, the spoofing warning, the rate-limited audit and
  [docs/auth/proxy-auth.md](../auth/proxy-auth.md).

## Consequences

- The safety of the feature rests on the deployment: the app port must be reachable only by
  the proxy, and the proxy must remove or overwrite client-supplied identity headers.
  Docker's userland port proxy and rootless networking can make outside connections arrive
  from the network gateway, so the documentation recommends trusting only the proxy's own
  address and not publishing the app port.
- Moving forwarded-header handling from uvicorn's configuration into `PeerMiddleware` keeps the
  behaviour but ties us to uvicorn's `ProxyHeadersMiddleware` import path and signature; a test
  fails if either changes. Running `uvicorn` directly (instead of `assetboard serve`) applies
  uvicorn's defaults and disables proxy authentication.
- A different proxy that sends the same user names resolves them to the same accounts: the
  subject is not tied to a particular proxy.
- Risk accepted: a proxy that is misconfigured to pass a client's `Remote-User` through lets
  that client sign in as anyone. The app cannot detect this.
