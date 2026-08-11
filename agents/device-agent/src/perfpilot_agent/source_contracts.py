"""Semantic limits for source-task wire documents.

The JSON Schemas close document shape.  This module enforces limits expressed
in canonical UTF-8 bytes so the eventual signed-task boundary and its tests use
the same implementation.
"""

from __future__ import annotations

import json


class SourceContractError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("source task contract is invalid")


def canonical_source_contract_bytes(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SourceContractError from None


def validate_source_contract_semantics(
    schema_name: str,
    document: dict[str, object],
) -> None:
    """Validate source-task byte budgets without exposing document content."""

    try:
        payload: object = document
        if schema_name == "agents/task-poll-response.schema.json":
            payload = document.get("snapshot")
            if not isinstance(payload, dict):
                return
        if isinstance(payload, dict) and payload.get("task_type") == "patch_verification":
            patch = payload.get("patch")
            if isinstance(patch, str) and len(patch.encode("utf-8")) > 65_536:
                raise SourceContractError

        if schema_name != "agents/source-task-completion.schema.json":
            return
        if len(canonical_source_contract_bytes(document)) > 128 * 1024:
            raise SourceContractError
        if document.get("task_type") != "source_context" or document.get("state") != "completed":
            return
        result = document.get("result")
        if not isinstance(result, dict):
            return
        fragments = result.get("fragments")
        if not isinstance(fragments, list):
            return
        snapshot_hash = result.get("snapshot_hash")
        if not isinstance(snapshot_hash, str) or any(
            not isinstance(fragment, dict)
            or fragment.get("snapshot_hash") != snapshot_hash
            for fragment in fragments
        ):
            raise SourceContractError
        fragment_bytes = sum(
            len(fragment["content"].encode("utf-8"))
            for fragment in fragments
            if isinstance(fragment, dict) and isinstance(fragment.get("content"), str)
        )
        if fragment_bytes > 98_304:
            raise SourceContractError
    except SourceContractError:
        raise
    except (TypeError, UnicodeError):
        raise SourceContractError from None


__all__ = [
    "SourceContractError",
    "canonical_source_contract_bytes",
    "validate_source_contract_semantics",
]
