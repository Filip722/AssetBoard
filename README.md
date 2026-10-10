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
