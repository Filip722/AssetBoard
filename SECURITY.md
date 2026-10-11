# Security policy

## Supported versions

During pre-alpha, security fixes are made against the current default branch. There are no stable
release lines yet.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's **Report a vulnerability**
control in the repository's Security tab (GitHub Security Advisories). Do not open a public issue
for an undisclosed vulnerability. Include the affected commit or version, impact, and steps to
reproduce when safe to do so. The maintainers will coordinate a response and disclosure there.

## Security model

AssetBoard uses local accounts with Argon2id password hashes, server-side sessions, CSRF tokens on
every form, and per-account and per-address sign-in throttling; first-run setup is protected by a
one-time setup token. The design and its accepted risks are recorded in
[ADR-0012](docs/adr/0012-local-authentication.md). Run it behind HTTPS and a correctly configured
reverse proxy as described in the [deployment guide](docs/deployment.md#reverse-proxies). The
accepted risks listed in ADR-0012 are known limitations, not vulnerability reports.
