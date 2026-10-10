# AssetBoard

A self-hosted personal finance web app for budgeting, net worth, investment, loan, and FIRE planning.

## License

This project is licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).

## Development

```bash
make setup
make dev
```

`make dev` builds the local CSS, compiles the translations, and starts the reload-enabled
development server at `http://127.0.0.1:8000`. Override the bind address with `DEV_HOST` and
`DEV_PORT`, for example `make dev DEV_PORT=8080`.

The web shell serves HTMX, Alpine.js (CSP build), and ECharts from pinned local vendor files.
Jinja2 provides server-rendered templates; Babel provides locale-aware number parsing, formatting,
and gettext catalog tooling.

Run the complete local checks with:

```bash
make check
```

## Run with Docker Compose

From a checkout, build and start the example deployment with:

```bash
docker compose -f compose.example.yaml up -d --build
```

Open <http://localhost:8000>. Compose keeps the database and generated secret key in the named
`assetboard-data` volume. To use a published image instead, set `ASSETBOARD_IMAGE` to its GHCR
reference and run Compose with `--no-build`:

```bash
ASSETBOARD_IMAGE=ghcr.io/your-github-owner/your-repository:latest \
  docker compose -f compose.example.yaml up -d --no-build
```

To expose the app on your LAN, set `ASSETBOARD_BIND_ADDRESS=0.0.0.0` (and optionally
`ASSETBOARD_HOST_PORT` to choose the host port), or publish it through a reverse proxy. There is
no authentication until M1, so restrict access to trusted users.

The example runs as UID 10001 with a read-only root filesystem and all Linux capabilities dropped.
`/data` is persistent and `/tmp` is writable. See the environment section in
`compose.example.yaml` to customize the locale, base currency, log format, trusted proxies, and
public URL. The first start creates a secret key in the data volume and applies database migrations
automatically.

For a bind mount, create the host directory and give it to the container user before starting:

```bash
mkdir -p ./assetboard-data
sudo chown 10001:10001 ./assetboard-data
```

Then replace `assetboard-data:/data` in the Compose file with `./assetboard-data:/data`. Startup
checks that `DATA_DIR` is writable and prints this ownership guidance if the mount permissions are
wrong. The published Compose example binds to `127.0.0.1`; put it behind a reverse proxy for public
access. See [deployment notes](docs/deployment.md) for Caddy/Traefik, release token behavior, and
making a GHCR package public.
