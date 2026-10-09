.PHONY: setup dev check test css migrate revision i18n-extract i18n-update i18n-compile

UV := uv

setup:
	$(UV) sync --all-groups
	$(UV) run pre-commit install

check:
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
	@echo "not yet implemented"

migrate:
	$(UV) run assetboard migrate

revision:
	$(UV) run assetboard revision --message "$(m)"

i18n-extract:
	@echo "not yet implemented"

i18n-update:
	@echo "not yet implemented"

i18n-compile:
	@echo "not yet implemented"

dev:
	@echo "not yet implemented"
