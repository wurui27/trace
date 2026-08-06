"""Canonical Android memory capture and pinned upstream result contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


_AndroidPackage = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
    ),
]
_AndroidRelease = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$",
    ),
]
ArtifactRole = Literal[
    "auto",
    "handoff_archive",
    "meminfo",
    "smaps",
    "showmap",
    "hprof",
    "gfxinfo",
    "proc_meminfo",
    "pressure_memory",
    "zram",
    "dmabuf",
    "exit_info",
    "analysis_report",
    "comparison_report",
    "perfetto_trace",
    "native_heap_profile",
    "phase_metadata",
    "device_context",
    "previous_ai_context",
    "previous_analysis_report",
    "android_log",
    "qa_screenshot",
]
_SingletonArtifactRole = Literal[
    "handoff_archive",
    "meminfo",
    "smaps",
    "showmap",
    "hprof",
    "gfxinfo",
    "proc_meminfo",
    "pressure_memory",
    "zram",
    "dmabuf",
    "exit_info",
    "perfetto_trace",
    "native_heap_profile",
    "phase_metadata",
    "device_context",
]
_SINGLETON_ARTIFACT_ROLES: frozenset[_SingletonArtifactRole] = frozenset(
    {
        "handoff_archive",
        "meminfo",
        "smaps",
        "showmap",
        "hprof",
        "gfxinfo",
        "proc_meminfo",
        "pressure_memory",
        "zram",
        "dmabuf",
        "exit_info",
        "perfetto_trace",
        "native_heap_profile",
        "phase_metadata",
        "device_context",
    }
)


class _FrozenManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class MemorySubject(_FrozenManifestModel):
    package: _AndroidPackage
    pid: int | None = Field(default=None, strict=True, ge=1, le=2_147_483_647)
    android_release: _AndroidRelease | None = None
    android_sdk: int | None = Field(default=None, strict=True, ge=1, le=100)


class MemoryArtifactRef(_FrozenManifestModel):
    artifact_id: UUID
    role: ArtifactRole


class MemoryCaptureManifest(_FrozenManifestModel):
    schema_version: Literal["1.0"]
    analysis_id: UUID
    capture_id: UUID
    phase: Literal["single", "before", "after", "cooldown"]
    source: Literal["manual_upload", "adb_agent"]
    captured_at: datetime | None = None
    subject: MemorySubject
    artifacts: tuple[MemoryArtifactRef, ...] = Field(min_length=1, max_length=2048)

    @field_validator("captured_at", mode="before")
    @classmethod
    def _require_timestamp_text_or_datetime(cls, value: object) -> object:
        if value is not None and not isinstance(value, str | datetime):
            raise ValueError("captured_at must be an RFC3339 string or datetime")
        return value

    @field_validator("captured_at")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _require_unique_artifacts(self) -> MemoryCaptureManifest:
        artifact_ids = tuple(artifact.artifact_id for artifact in self.artifacts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact IDs must be unique")

        singleton_roles = tuple(
            artifact.role
            for artifact in self.artifacts
            if artifact.role in _SINGLETON_ARTIFACT_ROLES
        )
        if len(set(singleton_roles)) != len(singleton_roles):
            raise ValueError("singleton artifact roles must be unique")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def sha256_hex(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class _FrozenUpstreamModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, hide_input_in_errors=True)


class AndroidMemoryGenerator(_FrozenUpstreamModel):
    name: Literal["android-memory-ai"]
    version: Literal["1.2.0"]


class AndroidMemoryPrivacy(_FrozenUpstreamModel):
    raw_contents_embedded: Literal[False]
    local_paths_included: Literal[False]

    @field_validator("raw_contents_embedded", "local_paths_included", mode="before")
    @classmethod
    def _require_actual_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Android memory privacy flags must be false")
        return value


class AndroidMemoryAnalysisContract(_FrozenUpstreamModel):
    support_level: Literal["insufficient", "limited", "supported", "strong"]
    primary_intent_support_level: Literal["insufficient", "limited", "supported", "strong"]
    privacy: AndroidMemoryPrivacy


class AndroidMemoryContext(_FrozenUpstreamModel):
    context_type: Literal["android-memory-ai-context"]
    schema_version: Literal["1.2"]
    generator: AndroidMemoryGenerator
    analysis_contract: AndroidMemoryAnalysisContract


__all__ = [
    "AndroidMemoryAnalysisContract",
    "AndroidMemoryContext",
    "AndroidMemoryGenerator",
    "AndroidMemoryPrivacy",
    "ArtifactRole",
    "MemoryArtifactRef",
    "MemoryCaptureManifest",
    "MemorySubject",
]
