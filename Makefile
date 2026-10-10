.PHONY: setup dev check test css migrate revision i18n-extract i18n-update i18n-compile

UV := uv
DEV_HOST ?= 127.0.0.1
DEV_PORT ?= 8000

setup:
	$(UV) sync --all-groups
	$(UV) run pre-commit install
	$(MAKE) css i18n-compile

check: i18n-compile
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run pyright
	$(UV) run pytest
	@temp_dir=$$(mktemp -d); \
	trap 'rm -rf "$$temp_dir"' EXIT; \
	ASSETBOARD_DATA_DIR="$$temp_dir" ASSETBOARD_DATABASE_URL="sqlite:///$$temp_dir/check.db" $(UV) run assetboard migrate && \
	ASSETBOARD_DATA_DIR="$$temp_dir" ASSETBOARD_DATABASE_URL="sqlite:///$$temp_dir/check.db" $(UV) run assetboard check-migrations
	$(UV) run pre-commit run --all-files

test:
	$(UV) run pytest

css:
	@mkdir -p src/assetboard/static/css .cache
	@set -eu; \
	case "$$(uname -s)-$$(uname -m)" in \
		Linux-x86_64) asset=tailwindcss-linux-x64; expected_sha=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;; \
		Linux-aarch64|Linux-arm64) asset=tailwindcss-linux-arm64; expected_sha=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;; \
		Darwin-x86_64) asset=tailwindcss-macos-x64; expected_sha=6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3 ;; \
		Darwin-arm64) asset=tailwindcss-macos-arm64; expected_sha=a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3 ;; \
		*) echo "Unsupported Tailwind CLI platform: $$(uname -s)-$$(uname -m)" >&2; exit 1 ;; \
	esac; \
	cache=".cache/tailwindcss-v3.4.17-$${asset#tailwindcss-}"; \
	file_sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$$1" | awk '{print $$1}'; else shasum -a 256 "$$1" | awk '{print $$1}'; fi; }; \
	if [ ! -x "$$cache" ] || [ "$$(file_sha256 "$$cache")" != "$$expected_sha" ]; then \
		temp="$$cache.tmp"; \
		curl --fail --location --silent --show-error \
			"https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/$$asset" \
			-o "$$temp"; \
		actual_sha=$$(file_sha256 "$$temp"); \
		if [ "$$actual_sha" != "$$expected_sha" ]; then rm -f "$$temp"; echo "Tailwind CLI checksum mismatch" >&2; exit 1; fi; \
		chmod +x "$$temp"; \
		mv "$$temp" "$$cache"; \
	fi; \
	"$$cache" --help | grep -Fq 'tailwindcss v3.4.17'; \
	"$$cache" --config tailwind.config.js \
		--input src/assetboard/static/css/input.css \
		--output src/assetboard/static/css/app.css --minify

migrate:
	$(UV) run assetboard migrate

revision:
	$(UV) run assetboard revision --message "$(m)"

i18n-extract:
	mkdir -p locales
	$(UV) run pybabel extract --no-location -F babel.cfg -k _ -k gettext \
		-o locales/messages.pot src/assetboard

i18n-update:
	$(MAKE) i18n-extract
	$(UV) run pybabel update --ignore-pot-creation-date -i locales/messages.pot -d src/assetboard/locales -l en
	$(UV) run pybabel update --ignore-pot-creation-date -i locales/messages.pot -d src/assetboard/locales -l pl

i18n-compile:
	$(UV) run pybabel compile -d src/assetboard/locales

dev:
	$(MAKE) css i18n-compile
	$(UV) run uvicorn assetboard.app:create_app --factory --reload --host $(DEV_HOST) --port $(DEV_PORT)
