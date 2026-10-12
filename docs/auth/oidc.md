# Sign in with OIDC

AssetBoard can let people sign in with an OpenID Connect provider such as Authentik, Keycloak,
Authelia or Google, next to (or instead of) passwords. It uses the Authorization Code flow with
PKCE against one provider. The design and its accepted risks are in
[ADR-0014](../adr/0014-oidc-sign-in.md); every setting is listed in
[docs/deployment.md](../deployment.md#environment-variables).

This page says what the provider must do and what to set in AssetBoard. It does not describe
the provider's own screens, which change between versions: follow your provider's documentation
for those steps.

## Checklist

1. **Serve AssetBoard over HTTPS** and set `ASSETBOARD_BASE_URL` to its public URL, for example
   `https://money.example.com`. OIDC refuses to start without an explicit `BASE_URL`, and
   requires `https://` unless `ASSETBOARD_ENV=dev`.
2. **Create a client (application) at the provider** with:
   - Client type: confidential (with a client secret). A public client without a secret works
     too, because AssetBoard always uses PKCE (S256).
   - Grant: Authorization Code. Response mode: query (the default).
   - Redirect URI: `<BASE_URL>/auth/oidc/callback`, for example
     `https://money.example.com/auth/oidc/callback`. It must match exactly.
   - Post-logout redirect URI (only with `ASSETBOARD_OIDC_RP_LOGOUT=true`): `<BASE_URL>/login`.
   - Scopes: at least `openid email profile`.
   - Token signing: an asymmetric algorithm (RS256 is the usual default; RSA-PSS, ECDSA and
     EdDSA also work). HMAC-signed (HS256) and unsigned ID tokens are refused.
3. **Make sure the provider sends a verified email.** AssetBoard reads `email` and
   `email_verified` from the ID token, or from the userinfo endpoint when the ID token lacks
   them. Only `email_verified: true` (or the string `"true"`) counts. Without it, a person can
   still sign in with an identity that is already linked, but cannot get a new account, accept
   an invitation, be linked to an existing account by email, or pass the domain allow-list.
4. **Set the AssetBoard settings:**

   ```env
   ASSETBOARD_BASE_URL=https://money.example.com
   ASSETBOARD_OIDC_ISSUER=https://auth.example.com/...   # exactly as the provider states it
   ASSETBOARD_OIDC_CLIENT_ID=assetboard
   ASSETBOARD_OIDC_CLIENT_SECRET=...                      # never logged
   ASSETBOARD_OIDC_PROVIDER_NAME=Authentik                # the button label
   ```

   The issuer must be exactly the `issuer` value in the provider's discovery document
   (`<issuer>/.well-known/openid-configuration`), trailing slash included. AssetBoard compares
   the two strings and refuses to sign anyone in when they differ.
5. **Choose who may sign in** (all off by default):
   - `ASSETBOARD_OIDC_AUTO_CREATE=true`: anyone the provider authenticates with a verified email
     gets an account with their own workspace.
   - Without it, people need an invitation link: "Continue with <provider>" on the invite page
     creates the account and joins that workspace.
   - `ASSETBOARD_OIDC_ALLOWED_DOMAINS=example.com,family.example`: only these email domains
     (exact match, verified email) may sign in with OIDC, checked at every sign-in.
   - `ASSETBOARD_OIDC_LINK_BY_EMAIL=true`: a first OIDC sign-in whose verified email matches an
     existing account offers to link them, after that account's password is entered once.
     Linking signs the account out everywhere else. Accounts without a password are never
     linked by email.
6. **Try it:** sign out, choose "Continue with <provider>" on the sign-in page, and check the
   audit log (`auth.oidc_failure` events carry a reason such as `email_unverified`) if it does
   not work. Reasons decided before the provider accepted the code (token endpoint errors
   included: an expired or replayed flow, a state or issuer mismatch, an error returned by the
   provider, a code the provider refused) are in the application log only.
7. **Optionally turn off passwords** with `ASSETBOARD_OIDC_ONLY=true` (see below).

## Administrators from a group

Set `ASSETBOARD_OIDC_ADMIN_GROUP` to a group name, and members of that group become instance
administrators when they sign in with OIDC; people outside it lose that role. A missing or
malformed groups claim changes nothing (and logs a warning), and the last active administrator
is never demoted. Workspace roles are not mapped: they come from invitations and the members
page.

Many providers include groups only when the client requests a groups scope, or only after you
add a groups mapper or claim to the client. Add that scope to `ASSETBOARD_OIDC_SCOPES` (for
example `openid email profile groups`) and set `ASSETBOARD_OIDC_GROUPS_CLAIM` if the claim is
not called `groups`. Check one ID token or userinfo response to see what your provider sends:
the claim must be a list of strings.

## Password sign-in and break-glass

`ASSETBOARD_OIDC_ONLY=true` hides password sign-in and local sign-up on invite pages, and
answers 404 to their form posts. It cannot be combined with `ASSETBOARD_ALLOW_REGISTRATION`.
Before turning it on, make sure an administrator can sign in through the provider.

`ASSETBOARD_ALLOW_LOCAL_ADMIN_LOGIN=true` keeps password sign-in for instance administrators
at `/login/local`, which no page links to. It has the same throttling and the same error
message as `/login`; a non-administrator's correct password fails exactly like a wrong one. If
the provider is down and you did not enable it, `assetboard create-admin` still works from the
command line.

## Signing out at the provider

With `ASSETBOARD_OIDC_RP_LOGOUT=true`, signing out of a session that started with OIDC ends the
AssetBoard session and then sends the browser to the provider's `end_session_endpoint` with
`client_id` and `post_logout_redirect_uri=<BASE_URL>/login`. Register that URI at the
provider. Providers that publish no `end_session_endpoint` get a normal sign-out.

There is no back-channel logout: disabling someone at the provider does not end their
AssetBoard sessions, which last until they expire or an administrator deactivates the account.

## Network: proxies and private certificate authorities

AssetBoard calls the provider itself (discovery, keys, token and userinfo) with Python's
standard library, with 10-second timeouts and without following redirects.

- **Outbound proxy.** The standard environment variables `HTTPS_PROXY` (and `NO_PROXY` for
  exceptions) are honoured. Set them on the container if it can only reach the provider through
  a proxy.
- **Private certificate authority.** The system trust store is used. If your provider's
  certificate comes from your own CA, mount a PEM bundle that contains it and set
  `SSL_CERT_FILE` to its path. That file replaces the default bundle, so include the public
  CAs too if the container also needs them (for example by appending your CA to a copy of the
  image's `/etc/ssl/certs/ca-certificates.crt`).

## Provider notes

These sections list what to look for; follow each provider's documentation for the steps.

### Authentik

- Create an OAuth2/OpenID provider and an application that uses it
  ([Authentik documentation](https://docs.goauthentik.io/)).
- Use the issuer exactly as the provider's discovery document states it, including any
  trailing slash.
- Check what the provider sends as `email_verified` for your users. If it is not `true`,
  account creation, invitations, linking and the domain allow-list refuse those users (see the
  checklist). Adjust the scope mappings or user settings in Authentik so that verified
  addresses are reported as verified.
- For administrators from a group, make sure the groups claim is part of the token or userinfo
  response for the scopes AssetBoard requests.

### Keycloak

- Create an OpenID Connect client in the realm that holds your users, with standard flow
  (authorization code) enabled ([Keycloak documentation](https://www.keycloak.org/documentation)).
- The issuer is the realm's URL; copy it exactly from the realm's discovery document.
- Keycloak reports `email_verified` from each user's "email verified" state. Users whose email
  was never verified are refused for account creation, invitations, linking and the domain
  allow-list. Verify their addresses in Keycloak first.
- Groups are not in the token by default: add a mapper that puts group membership into the
  token or userinfo response, and note the claim name it uses.

### Authelia

- Register AssetBoard as an OpenID Connect client in Authelia's configuration
  ([Authelia documentation](https://www.authelia.com/)).
- Use the issuer from Authelia's discovery document and the redirect URI from the checklist.
- Check what Authelia sends as `email_verified` for your users, and request the scope that
  carries groups if you use `ASSETBOARD_OIDC_ADMIN_GROUP`.

### Google and other providers

Any provider with discovery, the authorization code flow, PKCE and asymmetric ID token
signatures should work with the checklist above.

## When the issuer URL changes

Linked identities are stored per issuer, so after a change of `ASSETBOARD_OIDC_ISSUER` (a new
provider, or a new realm or hostname) existing links no longer match:

- **Accounts that have a password** sign in with it, or link again: with
  `ASSETBOARD_OIDC_LINK_BY_EMAIL=true`, the next OIDC sign-in asks for that password once.
- **Accounts created through OIDC have no password** and are never linked by email. An
  administrator first gives them one with `assetboard reset-password <email>`; the person then
  links the new provider with that password, or signs in with it and links the provider from
  the profile ([docs/accounts.md](../accounts.md#sign-in-methods)).

## Logs

The callback URL carries a one-time authorization code. AssetBoard's own request log never
records query strings, and `/auth/oidc/*` pages send no `Referer`. Reverse-proxy access logs
can record `/auth/oidc/callback?code=…&state=…`; the code is useless without the browser's
flow cookie and PKCE verifier and expires within minutes, but exclude these query strings from
proxy logs if you can. Rate-limit `/auth/oidc/` at the proxy as described in
[docs/deployment.md](../deployment.md#reverse-proxies).
