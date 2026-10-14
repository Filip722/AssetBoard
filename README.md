# AssetBoard

**Status: pre-alpha.** AssetBoard is a self-hosted personal-finance web app for tracking monthly
income and expenses, comparing planned with actual spending, and planning net worth, investments,
loans, and financial independence. It aims to give homelab users a private, user-controlled
alternative to spreadsheets and hosted finance services.

**Current state:** local accounts, first-run setup and the app shell work today; finance features
arrive with their milestones.

## Quick start

Docker Engine and Docker Compose v2 are required. Compose is the simplest way to run the current
foundation:

```bash
docker compose -f compose.example.yaml up -d --build
```

Open <http://localhost:8000> and complete first-run setup. The setup form asks for a one-time
setup token, which the container prints once in its log at startup
(`docker compose -f compose.example.yaml logs assetboard | grep "setup token"`), unless you set
`ASSETBOARD_SETUP_TOKEN`. You can also create the administrator from the command line with
`docker compose -f compose.example.yaml exec assetboard assetboard create-admin`. Compose stores the
database and generated secret in a named volume. See the [deployment guide](docs/deployment.md) for
reverse proxies (recommended before exposing the app beyond your machine), bind mounts, image
publishing, and the complete environment-variable reference.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSETBOARD_BASE_URL` | `http://localhost:8000` | Public URL used by the app |
| `ASSETBOARD_DEFAULT_LOCALE` | `en` | Default UI language |
| `ASSETBOARD_DEFAULT_CURRENCY` | `EUR` | Default currency |
| `ASSETBOARD_LOG_FORMAT` | `json` in Compose | `text` or `json` logs |
| `ASSETBOARD_TRUSTED_PROXIES` | empty | Proxy IPs/CIDRs allowed to supply forwarded headers; empty trusts no proxy |
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
`http://127.0.0.1:8000`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and checks, and
[docs/architecture.md](docs/architecture.md) for the layers and how workspace data is isolated.

## User guides

- [Your account](docs/accounts.md): profile, sign-in methods and the audit log.
- [Categories](docs/categories.md): expense and income categories, savings and transfers.

## Roadmap

See the [milestone roadmap](docs/specs/00-overview.md#5-roadmap-and-dependencies) and feature
specifications under `docs/specs/`.

## License

AssetBoard is licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
