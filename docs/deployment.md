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
| `ASSETBOARD_TRUSTED_PROXIES` | empty | App | Comma-separated proxy IPs/CIDRs allowed to supply forwarded headers. Empty trusts loopback only (`127.0.0.1`). |
| `ASSETBOARD_DEFAULT_LOCALE` | `en` | App | Two-letter lowercase default locale. |
| `ASSETBOARD_DEFAULT_CURRENCY` | `EUR` | App | Three-letter ISO currency used as the default. |
| `ASSETBOARD_AUTO_MIGRATE` | `true` | App | Apply pending database migrations at startup when enabled. Only one instance/worker may use a data volume with automatic migrations enabled. |
| `ASSETBOARD_BIND_ADDRESS` | `127.0.0.1` | Compose only | Host interface to publish. Use `0.0.0.0` to listen on all host interfaces. |
| `ASSETBOARD_HOST_PORT` | `8000` | Compose only | Host port mapped to the container's port 8000. |
| `ASSETBOARD_IMAGE` | `assetboard:local` | Compose only | Image reference interpolated by Compose. Set this when running a published image with `--no-build`. |

The Compose file intentionally binds to loopback by default. Publishing on all interfaces exposes
the service to the LAN; there is no authentication until M1, so restrict access accordingly.

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

The Compose file binds the service to loopback by default. Configure a reverse proxy on the same
Docker network and set `ASSETBOARD_BASE_URL` to the public URL. When the proxy forwards client
addresses, set `ASSETBOARD_TRUSTED_PROXIES` to the proxy's IP or CIDR so Uvicorn accepts forwarded
headers only from that peer range.

## Image tags

For stable `v0.1.0`, the published tags are `0.1.0`, `0.1`, and `latest`. Stable `v1.2.3` publishes
`1.2.3`, `1.2`, `1`, and `latest`. The major-only tag is omitted for 0.x releases. Prerelease
`v1.0.0-rc.1` publishes only `1.0.0-rc.1`; prereleases do not move `latest`.

## Rehearsing a release image

To rehearse a release image, open **Actions → Docker → Run workflow**, enter the existing release
tag, and leave **Publish the image to GHCR** unchecked. The workflow builds and tests both
architectures without logging in to GHCR or publishing. Check **Publish the image to GHCR** only
when you intend to upload the image and its release tags.
