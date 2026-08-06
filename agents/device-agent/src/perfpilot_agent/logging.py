from __future__ import annotations

import logging
import re
from collections.abc import Collection
from threading import RLock

_TOKEN_PATTERN = re.compile(r"\b(?:ppat|pprt|ppreg)_[A-Za-z0-9_-]{10,128}\b")
_URL_QUERY_PATTERN = re.compile(r"\bhttps://[^\s?#]+\?[^\s]+")


class SecretRedactor:
    def __init__(self) -> None:
        self._lock = RLock()
        self._live_values: tuple[str, ...] = ()

    def replace_live_values(
        self,
        *,
        serials: Collection[str],
        secrets: Collection[str],
    ) -> None:
        values = {
            value for value in (*serials, *secrets) if isinstance(value, str) and len(value) >= 4
        }
        with self._lock:
            self._live_values = tuple(sorted(values, key=len, reverse=True))

    def redact(self, value: object) -> str:
        rendered = str(value)
        with self._lock:
            live_values = self._live_values
        for secret in live_values:
            rendered = rendered.replace(secret, "[redacted]")
        rendered = _TOKEN_PATTERN.sub("[redacted]", rendered)
        return _URL_QUERY_PATTERN.sub(
            lambda match: f"{match.group(0).split('?', 1)[0]}?[redacted]",
            rendered,
        )


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = "Agent log message unavailable"
        record.msg = self._redactor.redact(rendered)
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redactor.redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True


__all__ = ["RedactingFilter", "SecretRedactor"]
