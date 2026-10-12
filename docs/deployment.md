# Deployment guide

## Environment variables

Application settings use the `ASSETBOARD_` prefix in the environment. Values shown as Compose
defaults are set or interpolated by `compose.example.yaml`; application defaults apply when the app
is run directly. `ASSETBOARD_BIND_ADDRESS`, `ASSETBOARD_HOST_PORT`, and `ASSETBOARD_IMAGE` are
Compose inputs only and are not application settings.

| Variable | Default | Scope | Description |
| --- | --- | --- | --- |
| `ASSETBOARD_ENV` | `dev` (Compose: `prod`) | App | Runtime mode: `dev` or `prod`. |
| `ASSETBOARD_DATA_DIR` | `./data` (Compose: `/data`) | App | Directory for SQLite data and generated secret key; must be writable. |
| `ASSETBOARD_DATABASE_URL` | `<DATA_DIR>/assetboard.db` | App | SQLite URL; this foundation supports SQLite only. |
| `ASSETBOARD_SECRET_KEY` | unset | App | Optional explicit secret. If absent, a key is generated once and persisted under `DATA_DIR`. |
| `ASSETBOARD_BASE_URL` | `http://localhost:8000` | App | Public base URL. Set to the external URL when using a reverse proxy. |
| `ASSETBOARD_LOG_LEVEL` | `INFO` | App | Minimum log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `ASSETBOARD_LOG_FORMAT` | `text` (Compose: `json`) | App | Log output format: `text` or `json`. |
| `ASSETBOARD_TRUSTED_PROXIES` | empty | App | Comma-separated proxy IPs/CIDRs allowed to supply forwarded headers. Empty trusts no proxy: forwarded headers are ignored and the TCP peer is the client address. |
| `ASSETBOARD_DEFAULT_LOCALE` | `en` | App | Two-letter lowercase default locale. |
| `ASSETBOARD_DEFAULT_CURRENCY` | `EUR` | App | Three-letter ISO currency used as the default. |
| `ASSETBOARD_AUTO_MIGRATE` | `true` | App | Apply pending database migrations at startup when enabled. Only one instance/worker may use a data volume with automatic migrations enabled. |
| `ASSETBOARD_SETUP_TOKEN` | unset | App | First-run setup token (at least 22 characters; use a random value). If unset, a 256-bit token is generated at each process start and logged once at `WARNING` while no user exists. A configured token is never logged. |
| `ASSETBOARD_ALLOW_REGISTRATION` | `false` | App | Allow open self-registration; each registrant gets a new empty workspace they own. |
| `ASSETBOARD_SESSION_IDLE_TIMEOUT_DAYS` | `14` | App | Sessions expire after this many days without activity. Must not exceed the absolute timeout. |
| `ASSETBOARD_SESSION_ABSOLUTE_TIMEOUT_DAYS` | `60` | App | Sessions expire this many days after login regardless of activity. |
| `ASSETBOARD_AUTH_ACCOUNT_FREE_FAILURES` | `5` | App | Failed logins (and wrong current passwords when changing a password) per account before exponential backoff starts. A successful login resets this counter; it also counts as zero after 24 hours without failures. |
| `ASSETBOARD_AUTH_IP_FREE_FAILURES` | `20` | App | Failed logins per client address (IPv4 address or IPv6 /64) within the IP window before backoff starts. |
| `ASSETBOARD_AUTH_IP_WINDOW_MINUTES` | `60` | App | Window after which per-address login and setup-token failure counters decay. Successful logins do not reset them. |
| `ASSETBOARD_AUTH_THROTTLE_BASE_SECONDS` | `60` | App | First backoff delay; it doubles with each further failure. |
| `ASSETBOARD_AUTH_THROTTLE_MAX_SECONDS` | `900` | App | Maximum backoff delay (bounds how long an attacker can lock an account). |
| `ASSETBOARD_SETUP_THROTTLE_FREE_FAILURES` | `5` | App | Wrong setup-token attempts per client address before backoff starts. |
| `ASSETBOARD_REGISTRATION_IP_LIMIT` | `5` | App | Registration attempts (successful or not) allowed per client address within the registration window. |
| `ASSETBOARD_REGISTRATION_WINDOW_MINUTES` | `60` | App | Window for the registration attempt limit. |
| `ASSETBOARD_AUTH_MAX_CONCURRENT_HASHES` | `2` | App | Password hashes (Argon2id, about 64 MiB of memory each) computed at the same time, 1 to 8. At most 16 further sign-in requests wait in total; when 16 are already waiting, or a wait exceeds 10 seconds, they get HTTP 503 with `Retry-After`. Health checks are not affected. |
| `ASSETBOARD_OIDC_ISSUER` | unset | App | OIDC provider's issuer URL, exactly as its discovery document states it (trailing slash included). Setting it turns OIDC sign-in on; it must be `https://` unless `ASSETBOARD_ENV=dev`, and `ASSETBOARD_BASE_URL` must then be set explicitly (and `https://` outside dev). See [docs/auth/oidc.md](auth/oidc.md). |
| `ASSETBOARD_OIDC_CLIENT_ID` | unset | App | Client ID registered at the provider. Required with `OIDC_ISSUER`. The redirect URI to register is `<BASE_URL>/auth/oidc/callback`. |
| `ASSETBOARD_OIDC_CLIENT_SECRET` | unset | App | Client secret; never logged. Unset means a public client that relies on PKCE alone. |
| `ASSETBOARD_OIDC_SCOPES` | `openid email profile` | App | Space-separated scopes; must contain `openid`. Add your provider's groups scope when using `OIDC_ADMIN_GROUP`. |
| `ASSETBOARD_OIDC_PROVIDER_NAME` | `SSO` | App | Label on the sign-in button (1 to 40 characters). |
| `ASSETBOARD_OIDC_LINK_BY_EMAIL` | `false` | App | Link a first OIDC sign-in to an existing account with the same email, only when the provider says `email_verified` and after that account's password is entered once. |
| `ASSETBOARD_OIDC_AUTO_CREATE` | `false` | App | Create an account (with its own workspace) for an unknown identity with a verified email. Invitation links create accounts without it. |
| `ASSETBOARD_OIDC_ALLOWED_DOMAINS` | empty | App | Comma-separated email domains allowed to sign in with OIDC (exact match, verified email required). Empty allows any. Checked at every sign-in. |
| `ASSETBOARD_OIDC_GROUPS_CLAIM` | `groups` | App | Claim holding the user's groups; only read when `OIDC_ADMIN_GROUP` is set. |
| `ASSETBOARD_OIDC_ADMIN_GROUP` | unset | App | Members of this group become instance administrators at OIDC sign-in, and non-members lose it (never the last active one). A missing or malformed groups claim changes nothing. |
| `ASSETBOARD_OIDC_ONLY` | `false` | App | Hide password sign-in and registration. Needs `OIDC_ISSUER`; refuses to start with `ALLOW_REGISTRATION`. |
| `ASSETBOARD_ALLOW_LOCAL_ADMIN_LOGIN` | `false` | App | Break-glass: with `OIDC_ONLY`, keep password sign-in at `/login/local` for instance administrators. No effect otherwise. |
| `ASSETBOARD_OIDC_RP_LOGOUT` | `false` | App | Signing out of an OIDC session also signs out at the provider (RP-initiated logout), if it publishes an `end_session_endpoint`. Register `<BASE_URL>/login` as the post-logout redirect URI. |
| `ASSETBOARD_BIND_ADDRESS` | `127.0.0.1` | Compose only | Host interface to publish. Use `0.0.0.0` to listen on all host interfaces. |
| `ASSETBOARD_HOST_PORT` | `8000` | Compose only | Host port mapped to the container's port 8000. |
| `ASSETBOARD_IMAGE` | `assetboard:local` | Compose only | Image reference interpolated by Compose. Set this when running a published image with `--no-build`. |

The Compose file intentionally binds to loopback by default. Publishing on all interfaces exposes
the sign-in page to the LAN; put a reverse proxy with HTTPS in front before exposing it further.

## First run and sign-in

While no user exists, every page redirects to `/setup`. The setup form asks for a setup token:
`ASSETBOARD_SETUP_TOKEN` if you set it (never logged), otherwise a random token generated at each
process start and printed once at `WARNING` in the application log. Anyone who can read the log
can complete setup until it is done, so finish setup promptly or set the token yourself. Setup
creates the administrator and the first workspace and signs you in; afterwards `/setup` returns
404 and signed-out visitors are sent to `/login`. `assetboard create-admin` (below) is an
alternative that needs no token.

Sessions are stored on the server. The session cookie lasts for the absolute session lifetime
(`ASSETBOARD_SESSION_ABSOLUTE_TIMEOUT_DAYS`); the idle timeout is enforced on the server. For MFA,
prefer OIDC or reverse-proxy auth backed by an IdP that enforces multi-factor authentication; TOTP
is intentionally deferred to the post-1.0 backlog. Run a single application process (one Uvicorn
worker): the setup token, the password-hashing limit and some caches live in the process.

### Invitations

Workspace owners add people from **Members** with an invitation link. AssetBoard sends no email:
copy the link (it is shown only once) and share it privately. The link works once and expires
after 1, 7 or 30 days. Whoever opens it can sign in, or create an account even when
`ASSETBOARD_ALLOW_REGISTRATION` is off; an account created this way joins only that workspace.
An invitation can be locked to an email address; owner invitations must be. The lock is not
proof that the person controls that address, since email addresses are not verified. See
[ADR-0013](adr/0013-invitation-links.md).

### Database schema state

The schema is checked once at startup. With `ASSETBOARD_AUTO_MIGRATE=true` (the default) an older
schema is upgraded first. With `ASSETBOARD_AUTO_MIGRATE=false` and an older schema, the process
still starts but no setup token is created, `/readyz` returns 503 with
`"database": "schema_outdated"`, and every page shows "The database needs an upgrade" with the
instruction to run `assetboard migrate` and then restart. If the database was written by a newer
version of AssetBoard, it is never changed: `/readyz` reports `"schema_newer"` and pages ask you to
install that version or a later one. `/healthz` stays green in both cases.

## Administrator commands

These commands run inside the container (for example `docker exec -it <container> assetboard
create-admin`) and need no setup token. With `ASSETBOARD_AUTO_MIGRATE=true` they apply pending
migrations first, like application startup; with `false` they refuse to run until
`assetboard migrate` has brought the database to the latest schema.

- `assetboard create-admin` is a break-glass command: it creates a new instance administrator even
  when users already exist. It never modifies an existing user; an email that is already
  registered is an error. By default it also creates a workspace owned by the new administrator,
  as first-run setup does; `--no-workspace` skips that. On an empty instance it also closes
  first-run setup. For scripts, pass `--email`, `--display-name` and `--password-stdin`.
- `assetboard reset-password <email>` sets a new password for any user (prompted, or from
  stdin with `--password-stdin`) and signs that user out of every session. It does not change whether the account is active. Anyone who can run commands
  in the container can therefore take over any account; protect shell access accordingly.

Both commands write an audit event with the CLI as the actor.

## Run a published image

After the first release has been published and the GHCR package has been made public, set
`ASSETBOARD_IMAGE` to a published version tag and start Compose without building locally:

```bash
ASSETBOARD_IMAGE=ghcr.io/lexanderpl/assetboard:0.1.0 \
  docker compose -f compose.example.yaml up -d --no-build
```

## Release automation credentials

The release-please workflow uses `GITHUB_TOKEN`. Ensure repository Actions settings allow
GitHub Actions to create and approve pull requests. GitHub suppresses CI workflows triggered by
events from `GITHUB_TOKEN`, so release PRs created with that token do not get ordinary push/PR CI
runs. Options are to require no checks on release PRs, use a PAT or GitHub App token, or close and
reopen the release PR to trigger its checks.

If release automation is changed to rely on a separate `push`-tag workflow, configure a
fine-grained PAT or GitHub App token with repository contents, issues, and pull-request permissions
for release-please. With a PAT, the created `v*` tag triggers the Docker workflow's `push` event as
well as the release workflow's `workflow_call`, which would publish twice. If using a PAT, remove
the `push.tags` trigger from `docker.yml` and keep the explicit `workflow_call` publish path.

## GHCR visibility

The first successful tagged release creates the container package in GitHub Container Registry.
To make images pullable without authentication, open the repository's **Packages** section, open
the container package settings, and change its visibility to **Public**. Confirm the package
belongs to the intended repository before changing visibility. Private packages remain usable by
users who authenticate to GHCR with an account that has package access.

## Data directory ownership

The container runs as UID/GID `10001:10001`. A named Docker volume is initialized for this user.
For a host bind mount, create the directory and run `chown 10001:10001` on it before starting
Compose. The startup check reports this command when `DATA_DIR` is not writable.

## Reverse proxies

> **Set `ASSETBOARD_TRUSTED_PROXIES` to your proxy's IP or CIDR.** Sign-in throttling counts
> failures per client address. The app only reads the TCP peer address that Uvicorn reports; Uvicorn
> replaces it with the forwarded client address only for requests from `ASSETBOARD_TRUSTED_PROXIES`.
> Without it, every visitor arriving through the proxy (or through Docker's port publishing, which
> can present the bridge gateway as the peer) shares one address, so one attacker can exhaust the
> per-address sign-in budget for everyone. Never list networks that untrusted clients can reach
> directly: they could then forge their address.

The Compose file binds the service to loopback by default. Configure a reverse proxy on the same
Docker network and set `ASSETBOARD_BASE_URL` to the public URL.

**HTTPS.** When `ASSETBOARD_BASE_URL` starts with `https://`, cookies are marked `Secure` and use the
`__Host-` prefix, so the app must then be reached over HTTPS only: browsers do not send those
cookies over plain HTTP (for example to a LAN address), and sign-in fails there. Use an `http://`
`BASE_URL` only for local or LAN-only setups.

**Origin check.** Forms are accepted only from the `BASE_URL` origin or from the origin the
request was addressed to (scheme plus `Host`), and never when the browser reports a cross-site
request. Make sure the proxy passes the original `Host` header and scheme.

**Rate limiting.** In addition to the built-in throttling, rate-limit `POST /login`,
`POST /register`, `/invite/` (invitation sign-up) and `/auth/oidc/` (OIDC sign-in, which can
create accounts) at the reverse proxy (for example nginx `limit_req`, the Traefik `RateLimit`
middleware, or a Caddy rate-limit module), such as a few requests per minute per client address.
This keeps password-guessing floods from reaching the app at all. Also cap the request body size
at the proxy (for example nginx `client_max_body_size 1m;`, the Traefik `Buffering` middleware's
`maxRequestBodyBytes`, or Caddy `request_body { max_size 1MB }`): the app does not yet limit body
sizes itself, and its forms need only a few kilobytes.

## Image tags

For stable `v0.1.0`, the published tags are `0.1.0`, `0.1`, and `latest`. Stable `v1.2.3` publishes
`1.2.3`, `1.2`, `1`, and `latest`. The major-only tag is omitted for 0.x releases. Prerelease
`v1.0.0-rc.1` publishes only `1.0.0-rc.1`; prereleases do not move `latest`.

## Rehearsing a release image

To rehearse a release image, open **Actions → Docker → Run workflow**, enter the existing release
tag, and leave **Publish the image to GHCR** unchecked. The workflow builds and tests both
architectures without logging in to GHCR or publishing. Check **Publish the image to GHCR** only
when you intend to upload the image and its release tags.

## Upgrading

### F1.3: OIDC sign-in

- **Nothing changes until `ASSETBOARD_OIDC_ISSUER` is set.** The migration adds the
  `external_identities` table and a `user_sessions.auth_method` column; existing sessions are
  recorded as password sign-ins.
- **New runtime dependencies:** Authlib and joserfc (with cryptography). With OIDC on, the
  container needs outbound HTTPS access to the provider; a private CA is trusted through
  `SSL_CERT_FILE` (see [docs/auth/oidc.md](auth/oidc.md)).
- **Settings that refuse to start:** `OIDC_ONLY` without `OIDC_ISSUER`, `OIDC_ONLY` with
  `ALLOW_REGISTRATION`, and OIDC without an explicit `BASE_URL` (or with an `http://` issuer or
  `BASE_URL` outside `ENV=dev`).
- **Before turning on `OIDC_ONLY`**, sign in once as an administrator through the provider, or
  set `ASSETBOARD_ALLOW_LOCAL_ADMIN_LOGIN=true` to keep `/login/local`.
- **Reverse-proxy logs record the callback query.** `/auth/oidc/callback?code=…&state=…` carries
  a one-time authorization code. It is useless without the browser's flow cookie and the PKCE
  verifier and expires within minutes, but exclude `/auth/oidc/` query strings from proxy logs
  if you can.

### F1.2: workspaces and invitations

- **Uvicorn's access log is off.** `assetboard serve` no longer prints uvicorn's access lines
  (`"GET /path HTTP/1.1" 200`): they show full URLs, and invitation links carry a token in the
  path and in `/login?next=…`. The application's own `request_complete` log line still records
  every request, with invitation tokens replaced by `[redacted]` and without query strings.
  There is no setting to turn the access log back on. If you parse access lines, switch to
  `request_complete`.
- **Reverse-proxy logs may still record invitation tokens.** A token can appear in the
  `/invite/<token>` path and in the query of `/login?next=%2Finvite%2F<token>`. Excluding `/invite/`
  from the proxy's access log is not enough: also exclude or strip query strings for `/login`
  and `/register`, and the `Referer` field if your log format records it (form submissions and
  links from these pages send a `Referer` to the app itself; their static files do not), or
  restrict who can read the proxy log. Invitations are
  single-use and expire, which limits the damage of a leaked token.

### F1.1: local accounts

- **`ASSETBOARD_TRUSTED_PROXIES` empty now trusts no proxy.** Earlier versions trusted forwarded
  headers from `127.0.0.1` when the setting was empty. If your reverse proxy runs on the same host
  and connects over loopback, set `ASSETBOARD_TRUSTED_PROXIES=127.0.0.1`; otherwise set it to the
  proxy's address or network.
- **First run needs setup.** After upgrading an existing instance, the app redirects to `/setup`
  until an administrator exists. Use the setup token from the log, or run
  `assetboard create-admin`.
