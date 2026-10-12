# Sign in through a reverse proxy (header authentication)

> [!WARNING]
> **Read this section before turning proxy authentication on.** With
> `ASSETBOARD_PROXY_AUTH_ENABLED=true`, a request that carries `Remote-User: alice` (or whatever
> header you configure) **is** Alice, if it comes from an address in
> `ASSETBOARD_TRUSTED_PROXIES`. Anyone who can make such a request can sign in as anyone.
>
> - **Only the proxy may reach AssetBoard's port.** Do not publish the container port on the
>   host or LAN. Put AssetBoard and the proxy on a Docker network that holds nothing else, and
>   list only the proxy's own address in `ASSETBOARD_TRUSTED_PROXIES`.
> - **The proxy must overwrite or remove the identity headers on every request**, including
>   headers the browser sent itself. The examples below show how, per proxy; check yours with
>   the [test](#test-it) before relying on it.
> - **The email header counts as verified.** Send only addresses your identity provider has
>   verified: accounts are created and linked by that address.
> - **Signing out of AssetBoard does not sign you out of the proxy.** The next page you open is
>   signed in again. Use your proxy's own logout URL to sign out everywhere.

AssetBoard reads the identity headers only when the TCP connection comes from an address in
`ASSETBOARD_TRUSTED_PROXIES`. `X-Forwarded-For` never makes a connection trusted. Identity
headers from any other address are ignored and logged at WARNING (header names only, at most
once per address range every 10 minutes); they are never audited, because anyone can send them.
The design and its accepted risks are in
[ADR-0015](../adr/0015-proxy-header-authentication.md).

## Settings

```env
ASSETBOARD_PROXY_AUTH_ENABLED=true
ASSETBOARD_PROXY_AUTH_USER_HEADER=Remote-User      # the stable user name
ASSETBOARD_PROXY_AUTH_EMAIL_HEADER=Remote-Email    # treated as verified
ASSETBOARD_PROXY_AUTH_NAME_HEADER=Remote-Name      # optional: names new accounts
ASSETBOARD_TRUSTED_PROXIES=172.30.0.2              # the proxy's own address
ASSETBOARD_PROXY_AUTH_AUTO_CREATE=false            # true: anyone let in gets an account
ASSETBOARD_PROXY_AUTH_LINK_BY_EMAIL=false          # true: link after the account's password
```

AssetBoard refuses to start with proxy authentication on when the user or email header is not
set, when two settings name the same header, when a header name is reserved (`Cookie`, `Host`,
`Authorization`, `X-Forwarded-For` and similar), when `ASSETBOARD_TRUSTED_PROXIES` is empty, or
when an entry is wider than IPv4 `/24` or IPv6 `/64`. Each entry that covers more than one
address is named in a startup warning. Run AssetBoard with `assetboard serve` (as the image
does): only then does it see which address a connection really came from.

## What the headers are trusted for

- **User header:** the account's identity at the proxy. The first sign-in links it to an
  account; from then on the user name alone signs in, even if the email changes.
- **Email header:** used only to create an account, accept an invitation or link to an existing
  account. A missing or malformed email still lets a linked user in, but creates and links
  nothing.
- **Name header:** the display name of a new account, nothing else.
- **Groups are not read.** Proxy sign-in never makes anyone an instance administrator or takes
  that role away.
- A header sent twice, or with control characters, is refused, and the request counts as signed
  out.

The OIDC settings do not apply to proxy sign-in: `ASSETBOARD_OIDC_ALLOWED_DOMAINS`,
`ASSETBOARD_OIDC_ADMIN_GROUP`, `ASSETBOARD_OIDC_AUTO_CREATE` and `ASSETBOARD_OIDC_LINK_BY_EMAIL`
are ignored for it. Limit who gets in at the proxy.

**Switching proxies keeps the accounts.** Linked user names are stored per method, not per
proxy, so a different proxy that sends the same user names signs in to the same accounts.
Make sure user names mean the same people at both.

## Network

- **Proxy on the same host, without Docker:** `ASSETBOARD_TRUSTED_PROXIES=127.0.0.1` (and
  `::1` if it connects over IPv6), with AssetBoard listening on loopback only.
- **Proxy in Docker:** give the proxy a fixed address on a network shared only with
  AssetBoard, and trust that address alone:

  ```yaml
  networks:
    auth:
      ipam:
        config:
          - subnet: 172.30.0.0/24
  services:
    proxy:
      networks:
        auth:
          ipv4_address: 172.30.0.2
    assetboard:
      environment:
        ASSETBOARD_TRUSTED_PROXIES: 172.30.0.2
      networks: [auth]
      # no "ports:" here
  ```

- **Do not publish AssetBoard's port.** With Docker's userland proxy, and with rootless Docker
  or Podman networking, connections to a published port can arrive from the network's gateway
  (for example `172.30.0.1`), which is inside a trusted subnet. Trusting the proxy's single
  address avoids this.
- **IPv6 link-local addresses with a zone** (`fe80::1%eth0`) are never trusted.

## Header stripping, per proxy

Each example passes the headers the authentication service returned and makes sure a value the
browser sent can never reach AssetBoard. Follow your proxy's and identity provider's own
documentation for everything else.

### nginx with `auth_request` (for example Authelia)

[Authelia's nginx guide](https://www.authelia.com/integration/proxies/nginx/) copies the
headers from the authentication response into variables and sets them for the backend:

```nginx
auth_request_set $user  $upstream_http_remote_user;
auth_request_set $email $upstream_http_remote_email;
auth_request_set $name  $upstream_http_remote_name;
proxy_set_header Remote-User  $user;
proxy_set_header Remote-Email $email;
proxy_set_header Remote-Name  $name;
```

`proxy_set_header` redefines the header for the backend, and an empty value means the header
is not passed at all ([nginx documentation][nginx-proxy-set-header]),
so whatever the browser sent is dropped. Keep these lines in the same block as your other
`proxy_set_header` lines: nginx inherits them from an outer level only if the current level
sets none.

### Caddy with `forward_auth`

```caddyfile
money.example.com {
	forward_auth authelia:9091 {
		uri /api/authz/forward-auth
		copy_headers Remote-User Remote-Email Remote-Name
	}
	reverse_proxy assetboard:8000
}
```

**Use Caddy 2.11.2 or later.** From 2.10.0 up to 2.11.1, `copy_headers` passed the browser's
own header through when the authentication service did not return one
([GHSA-7r4p-vjf4-gxv4][caddy-ghsa]);
2.11.2 is the patched version. See the
[`forward_auth` documentation](https://caddyserver.com/docs/caddyfile/directives/forward_auth)
and [Authelia's Caddy guide](https://www.authelia.com/integration/proxies/caddy/).

### Traefik with `forwardAuth`

```yaml
http:
  middlewares:
    authelia:
      forwardAuth:
        address: http://authelia:9091/api/authz/forward-auth
        authResponseHeadersRegex: ^Remote-
```

Traefik documents `authResponseHeaders` as copying the listed headers "replacing any existing
conflicting headers"; it does not say that a header the authentication response lacks is removed.
`authResponseHeadersRegex` sets the matching headers "after stripping all headers that match the
regex" ([Traefik `forwardAuth` reference][traefik-forwardauth]),
so prefer it.

### Authentik (proxy provider or forward auth)

Authentik sends `X-authentik-username`, `X-authentik-email` and `X-authentik-name`
([proxy provider documentation](https://docs.goauthentik.io/add-secure-apps/providers/proxy/)):

```env
ASSETBOARD_PROXY_AUTH_USER_HEADER=X-authentik-username
ASSETBOARD_PROXY_AUTH_EMAIL_HEADER=X-authentik-email
ASSETBOARD_PROXY_AUTH_NAME_HEADER=X-authentik-name
```

Authentik's documentation does not say whether a browser's own `X-authentik-*` headers are
removed. With forward auth, strip them in the proxy as shown above (nginx `proxy_set_header`,
Caddy 2.11.2+ `copy_headers`, Traefik `authResponseHeadersRegex: ^X-authentik-`). With the
proxy outpost in front, run the test below.

## Test it

Do this after every change to the proxy configuration. `money.example.com` is your public
address; use a user name that exists, such as an administrator's.

1. **A forged header without signing in must be challenged.** In a private browser window, or
   with no cookies:

   ```sh
   curl -si -H 'Remote-User: admin' -H 'Remote-Email: admin@example.com' https://money.example.com/
   ```

   The proxy must answer with its login page or a redirect to it (`302`, `401`), never with an
   AssetBoard page.
2. **A forged header while signed in as someone else must be overwritten.** Sign in at the
   proxy as a normal user, copy the proxy's session cookie from the browser, and ask for the
   sign-in page with a forged header:

   ```sh
   curl -s -b '<proxy session cookie>' -H 'Remote-User: admin' https://money.example.com/login \
     | grep 'Continue as'
   ```

   The sign-in page names the user AssetBoard received: it must be **your** user, never
   `admin`.
3. **A port reachable from elsewhere must not trust the headers.** From another host on the
   network, connect to AssetBoard's port directly, if it is reachable at all:

   ```sh
   curl -si -H 'Remote-User: admin' http://<assetboard-host>:8000/
   ```

   The answer must be a redirect to `/login`, and AssetBoard's log shows
   `proxy identity headers from an untrusted peer were ignored`. Better still, the connection
   is refused because the port is not published.

## Signing in and out

A person who already has an account (for example with a password) can link the proxy sign-in
from the profile page while browsing through the proxy; see
[docs/accounts.md](../accounts.md#sign-in-methods).

Opening any page while the proxy presents a user signs in through `/auth/proxy`. The sign-in
page and invitation pages also show "Continue as <user>". An invitation link opened through the
proxy joins that workspace, even without `ASSETBOARD_PROXY_AUTH_AUTO_CREATE`.

A proxy session lasts only while the proxy keeps presenting the same user. When the proxy sends
another user (someone else signed in at the proxy), AssetBoard ends the old session and signs in
the new user. Requests without the headers count as signed out, but the session stays, so a
proxy that skips authentication on some paths does not sign anyone out. Password and OIDC
sessions are not affected by proxy headers.

Signing out ends the AssetBoard session and shows the sign-in page, which does not sign in by
itself. Any other page signs in again as long as the proxy presents the user.

If the browser does not keep the session cookie (for example `ASSETBOARD_BASE_URL` starts with
`https://` but the site was opened over `http://`), sign-in stops at a page that says so instead
of redirecting in a loop.

## Monitors, scripts and other clients

Only a browser loading a page (`Sec-Fetch-Dest: document` or `Sec-Fetch-Mode: navigate`, or
`Accept: text/html` when the browser sends neither) is signed in through `/auth/proxy`. Other
requests without a session, such as `curl` or an image, get the plain redirect to `/login`
and create no session.

- **Point uptime monitors at `/healthz`** (the process is up) or `/readyz` (the database is
  ready; it answers 503 otherwise). Both need no sign-in.
- **Keep clients that cannot store cookies on those two URLs.** If such a client requests any
  other page with the headers a browser sends (`Accept: text/html`) and follows redirects,
  every request goes through `/auth/proxy` and creates a new session.

## Logs and audit

Refused proxy sign-ins (no account, deactivated, …) are audited as `auth.proxy_failure` with a
reason, at most once per user and reason every 10 minutes; repeats are logged only. These
counters live in memory and restart with the application. Rate-limit `/auth/proxy` at the proxy
as described in [docs/deployment.md](../deployment.md#reverse-proxies).

[nginx-proxy-set-header]: https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header
[caddy-ghsa]: https://github.com/caddyserver/caddy/security/advisories/GHSA-7r4p-vjf4-gxv4
[traefik-forwardauth]: https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/forwardauth/
