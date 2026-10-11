"""Redact credentials at terminal and logging boundaries."""

import logging
import os
import re
import traceback
from collections.abc import Mapping

_SECRET_NAME = re.compile(r"key|token|secret|password|credential|authorization", re.I)
_ASSIGNMENT = re.compile(
    r"(?i)([\w-]*(?:api[_-]?key|token|secret|password|credential|authorization)[\w-]*"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def redact(message: str, environ: Mapping[str, str] | None = None) -> str:
    values = environ if environ is not None else os.environ
    for value in sorted(
        {value for key, value in values.items() if value and _SECRET_NAME.search(key)},
        key=len,
        reverse=True,
    ):
        message = message.replace(value, "[REDACTED]")
    message = _BEARER.sub("Bearer [REDACTED]", message)
    return _ASSIGNMENT.sub(r"\1[REDACTED]", message)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Format first so arguments, exceptions, and stack information are covered.
        return redact(super().format(record))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def safe_error(exc: Exception) -> str:
    return redact("".join(traceback.format_exception_only(type(exc), exc)).strip())
