"""Stable contracts between the control plane and external engine adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import SecretStr


AnalysisProfile = Literal["auto", "startup", "scroll"]
ResourceProfile = Literal["network_service", "isolated_worker"]
RetryMode = Literal["reconnect", "new_attempt"]
ExecutionStateValue = Literal[
    "pending",
    "running",
    "awaiting_user",
    "completed",
    "insufficient_data",
    "failed",
    "canceled",
]
EngineTerminalStateValue = Literal[
    "completed",
    "insufficient_data",
    "failed",
    "canceled",
]


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    engine_id: str
    adapter_version: str
    profiles: frozenset[AnalysisProfile]
    required_inputs: frozenset[str]
    optional_inputs: frozenset[str]
    accepted_contracts: frozenset[str]
    default_timeout_seconds: int
    resource_profile: ResourceProfile
    stable_error_codes: frozenset[str]


@dataclass(frozen=True, slots=True)
class EngineInput:
    """Public artifact metadata plus a short-lived, secret download claim.

    ``download_url`` is ephemeral and must never be logged or persisted. Future
    claim construction belongs to the server-owned artifact record and claim path.
    """

    artifact_id: UUID
    kind: str
    mime: str
    size_bytes: int
    sha256_b64: str
    download_url: SecretStr


@dataclass(frozen=True, slots=True)
class SubmitConfig:
    analysis_id: UUID
    profile: AnalysisProfile
    question: str | None
    external_workspace_id: str | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class EngineRunRef:
    engine_id: str
    external_session_id: str | None
    external_run_id: str | None
    cursor: str | None
    external_workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class EngineEvent:
    event_id: str
    state: ExecutionStateValue
    progress_percent: int | None
    message_code: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EngineEventBatch:
    run_ref: EngineRunRef
    events: tuple[EngineEvent, ...]


@dataclass(frozen=True, slots=True)
class EngineStatus:
    run_ref: EngineRunRef
    state: ExecutionStateValue
    stable_error_code: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class EngineRetryDirective:
    mode: RetryMode
    execution_id: UUID
    attempt_number: int
    stable_error_code: str
    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class EngineStepOutcome:
    execution_id: UUID
    state: ExecutionStateValue
    retry: EngineRetryDirective | None


@dataclass(frozen=True, slots=True)
class EngineResult:
    contract: str
    state: EngineTerminalStateValue
    payload: dict[str, object]


class EngineAdapter(Protocol):
    descriptor: AdapterDescriptor

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef: ...

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> EngineEventBatch: ...

    async def status(self, run_ref: EngineRunRef) -> EngineStatus: ...

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult: ...

    async def cancel(self, run_ref: EngineRunRef) -> EngineTerminalStateValue: ...
