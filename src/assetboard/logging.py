from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from assetboard.branding import APP_NAME


class JsonFormatter(logging.Formatter):
    """Format log records as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for field in ("method", "path", "status_code", "duration_ms"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


def configure_logging(level: str, format_name: str) -> logging.Logger:
    logger = logging.getLogger(APP_NAME.lower())
    logger.setLevel(level)
    logger.propagate = False

    formatter = (
        JsonFormatter()
        if format_name == "json"
        else logging.Formatter("%(levelname)s request_id=%(request_id)s %(message)s")
    )
    if logger.handlers:
        for handler in logger.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(item, RequestContextFilter) for item in handler.filters):
                handler.addFilter(RequestContextFilter())
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(RequestContextFilter())
        logger.addHandler(handler)

    return logger
