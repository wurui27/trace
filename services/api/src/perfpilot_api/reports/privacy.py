"""Fail-closed privacy validation for data sent to an AI provider."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from urllib.parse import unquote


_MAX_DEPTH = 64
_MAX_NODES = 200_000
_MAX_STRING_CHARS = 2_000
_PRIVATE_PATTERNS = (
    re.compile(r"https?://[^/\s]+@", re.IGNORECASE),
    re.compile(r"https?://[^\s?#]+[^\s]*[?&](?:x-(?:amz|goog)-(?:signature|credential)|signature|token)=[^\s&]+", re.IGNORECASE),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+[^:]+)?|redis)://", re.IGNORECASE),
    re.compile(r"(?:s3|gs)://", re.IGNORECASE),
    re.compile(r"\b(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|access[_-]?key|secret|token|password|credential)\s*[:=]", re.IGNORECASE),
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"(?:^|[\s\"'])/(?:[^\s]+)"),
    re.compile(r"\b[A-Za-z]:[\\/]"),
    re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)"),
)
_PRIVATE_KEYS = {
    "accesskey",
    "accesstoken",
    "apikey",
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
}


class ProjectionPrivacyError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("projection contains private data")


def _decoded_text(value: str) -> str:
    decoded = unicodedata.normalize("NFKC", value)
    for _ in range(8):
        candidate = unicodedata.normalize("NFKC", unquote(decoded))
        if candidate == decoded:
            break
        decoded = candidate
    return decoded


def _private_text(value: str) -> bool:
    decoded = _decoded_text(value)
    return any(pattern.search(decoded) is not None for pattern in _PRIVATE_PATTERNS)


def _private_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _decoded_text(value).casefold())
    return normalized in _PRIVATE_KEYS


def reject_private_json(value: object) -> None:
    """Validate a JSON tree without ever retaining/redacting secret values."""

    nodes = 0
    ancestors: set[int] = set()

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise ProjectionPrivacyError
        if item is None or type(item) is bool or type(item) is int:
            return
        if type(item) is float:
            if math.isfinite(item):
                return
            raise ProjectionPrivacyError
        if type(item) is str:
            if len(item) > _MAX_STRING_CHARS or _private_text(item):
                raise ProjectionPrivacyError
            return
        if isinstance(item, Mapping):
            marker = id(item)
            if marker in ancestors:
                raise ProjectionPrivacyError
            ancestors.add(marker)
            try:
                for key, nested in item.items():
                    if type(key) is not str:
                        raise ProjectionPrivacyError
                    if _private_key(key):
                        raise ProjectionPrivacyError
                    visit(key, depth + 1)
                    visit(nested, depth + 1)
            finally:
                ancestors.remove(marker)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            marker = id(item)
            if marker in ancestors:
                raise ProjectionPrivacyError
            ancestors.add(marker)
            try:
                for nested in item:
                    visit(nested, depth + 1)
            finally:
                ancestors.remove(marker)
            return
        raise ProjectionPrivacyError

    visit(value, 0)


__all__ = ["ProjectionPrivacyError", "reject_private_json"]
