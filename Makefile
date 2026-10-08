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
	$(UV) run pre-commit run --all-files

test:
	$(UV) run pytest

css:
	@echo "not yet implemented"

migrate:
	@echo "not yet implemented"

revision:
	@echo "not yet implemented"

i18n-extract:
	@echo "not yet implemented"

i18n-update:
	@echo "not yet implemented"

i18n-compile:
	@echo "not yet implemented"

dev:
	@echo "not yet implemented"
