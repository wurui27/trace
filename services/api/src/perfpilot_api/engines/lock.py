from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


class EngineLockError(RuntimeError):
    pass


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
        lock_data = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
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
