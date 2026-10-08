from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from assetboard.branding import APP_NAME, ENV_PREFIX


@pytest.fixture(autouse=True)
def isolate_application_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv(f"{ENV_PREFIX}_DATA_DIR", str(tmp_path / "assetboard-data"))
    for name in (
        "DATABASE_URL",
        "SECRET_KEY",
        "BASE_URL",
        "ENV",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "TRUSTED_PROXIES",
        "DEFAULT_LOCALE",
        "DEFAULT_CURRENCY",
        "AUTO_MIGRATE",
    ):
        monkeypatch.delenv(f"{ENV_PREFIX}_{name}", raising=False)

    logger = logging.getLogger(APP_NAME.lower())
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    yield
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
