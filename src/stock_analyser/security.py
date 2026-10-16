"""Redact credentials at terminal and logging boundaries."""

import copy
import json
import logging
import os
import re
import traceback
from collections.abc import Mapping

_SECRET_NAME = re.compile(r"key|token|secret|password|credential|authorization", re.I)
_ASSIGNMENT = re.compile(
    r"(?i)([\w-]*(?:api[_-]?key|token|secret|password|credential|authorization)[\w-]*"
    r"[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def redact(message: str, environ: Mapping[str, str] | None = None) -> str:
    values = environ if environ is not None else os.environ
    secrets = {value for key, value in values.items() if value and _SECRET_NAME.search(key)}
    # Logs can contain already serialized JSON or Python representations. Match
    # their escaped forms too, even when the surrounding field is not secret-named.
    representations = {
        representation
        for secret in secrets
        for representation in (
            secret,
            json.dumps(secret, ensure_ascii=True)[1:-1],
            json.dumps(secret, ensure_ascii=False)[1:-1],
            repr(secret)[1:-1],
        )
    }
    for value in sorted(representations, key=len, reverse=True):
        message = message.replace(value, "[REDACTED]")
    message = _BEARER.sub("Bearer [REDACTED]", message)

    def replace_assignment(match: re.Match[str]) -> str:
        value = match.group(2)
        quote = value[0] if value[0] in "\"'" else ""
        return f"{match.group(1)}{quote}[REDACTED]{quote}"

    return _ASSIGNMENT.sub(replace_assignment, message)


def redact_values(value: object) -> object:
    """Sanitize structured values before converting them to JSON or log text."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_values(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_values(item) for item in value)
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Leave the original record intact for other handlers. Sanitize structured
        # messages/arguments before logging stringifies them, then cover serialized
        # messages, exceptions, and stack information at the output boundary.
        sanitized = copy.copy(record)
        if isinstance(record.msg, Mapping):
            sanitized.msg = redact_values(record.msg)
        sanitized.args = redact_values(record.args)
        return redact(super().format(sanitized))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def safe_error(exc: Exception) -> str:
    return redact("".join(traceback.format_exception_only(type(exc), exc)).strip())
