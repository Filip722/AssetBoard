# AssetBoard

**Status: pre-alpha.** AssetBoard is a self-hosted personal-finance web app for tracking monthly
income and expenses, comparing planned with actual spending, and planning net worth, investments,
loans, and financial independence. It aims to give homelab users a private, user-controlled
alternative to spreadsheets and hosted finance services.

**Current state:** only the app shell and health endpoints work today; finance features arrive with
their milestones.

## Quick start

Docker Engine and Docker Compose v2 are required. Compose is the simplest way to run the current
foundation:

```bash
docker compose -f compose.example.yaml up -d --build
```

Open <http://localhost:8000>. Compose stores the database and generated secret in a named volume.
**There is no authentication until M1; restrict access to trusted users.** See the
[deployment guide](docs/deployment.md) for bind mounts, reverse proxies, image publishing, and the
complete environment-variable reference.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSETBOARD_BASE_URL` | `http://localhost:8000` | Public URL used by the app |
| `ASSETBOARD_DEFAULT_LOCALE` | `en` | Default UI language |
| `ASSETBOARD_DEFAULT_CURRENCY` | `EUR` | Default currency |
| `ASSETBOARD_LOG_FORMAT` | `json` in Compose | `text` or `json` logs |
| `ASSETBOARD_TRUSTED_PROXIES` | empty | Proxy IPs/CIDRs allowed to supply forwarded headers; empty trusts loopback only |
| `ASSETBOARD_BIND_ADDRESS` | `127.0.0.1` | Compose-only host interface |
| `ASSETBOARD_HOST_PORT` | `8000` | Compose-only host port |

The full configuration table, including application defaults, is in the
[deployment guide](docs/deployment.md#environment-variables).

## Development

```bash
make setup
make dev
make check
```

`make dev` builds local CSS, compiles translations, and starts the reload-enabled server at
`http://127.0.0.1:8000`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and checks.

## Roadmap

See the [milestone roadmap](docs/specs/00-overview.md#5-roadmap-and-dependencies) and feature
specifications under `docs/specs/`.

## License

AssetBoard is licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
