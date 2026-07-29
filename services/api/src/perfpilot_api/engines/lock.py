from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


class EngineLockError(RuntimeError):
    pass


class _DuplicateKeyError(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_yaml_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if any(key_node.tag == "tag:yaml.org,2002:merge" for key_node, _ in node.value):
        raise _DuplicateKeyError

    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_yaml_mapping,
)


def _construct_unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateKeyError
        mapping[key] = value
    return mapping


@dataclass(frozen=True, slots=True)
class EnginePin:
    source: str
    ref: str | None
    commit: str
    image_digest: str | None
    contract: str


@dataclass(frozen=True, slots=True)
class EngineLock:
    schema_version: str
    smartperfetto: EnginePin
    android_memory: EnginePin


def load_engine_lock(
    path: Path,
    *,
    schema_path: Path,
    require_image_digests: bool,
) -> EngineLock:
    try:
        lock_data = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
        schema_data = json.loads(
            schema_path.read_text(encoding="utf-8"),
            object_pairs_hook=_construct_unique_json_object,
        )
        Draft202012Validator.check_schema(schema_data)
        Draft202012Validator(schema_data).validate(lock_data)

        engines = lock_data["engines"]
        smartperfetto = _engine_pin(engines["smartperfetto"], "api_contract")
        android_memory = _engine_pin(engines["android_memory"], "output_contract")
        lock = EngineLock(
            schema_version=lock_data["schema_version"],
            smartperfetto=smartperfetto,
            android_memory=android_memory,
        )
    except Exception as error:
        raise EngineLockError("engine lock is invalid") from error

    if require_image_digests and (
        lock.smartperfetto.image_digest is None or lock.android_memory.image_digest is None
    ):
        raise EngineLockError("production engine image digest is required")

    return lock


def _engine_pin(data: dict[str, Any], contract_key: str) -> EnginePin:
    return EnginePin(
        source=data["source"],
        ref=data["ref"],
        commit=data["commit"],
        image_digest=data["image_digest"],
        contract=data[contract_key],
    )
