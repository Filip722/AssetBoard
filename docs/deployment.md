# Container deployment notes

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
