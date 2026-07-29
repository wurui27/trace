from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    EngineEvent,
    EngineInput,
    EngineResult,
    EngineRunRef,
    SubmitConfig,
)
from perfpilot_api.engines.registry import AdapterRegistry, AdapterRegistryError


class FakeAdapter:
    descriptor = AdapterDescriptor(
        engine_id="smartperfetto",
        adapter_version="1.0.0",
        profiles=frozenset({"auto", "startup", "scroll"}),
        required_inputs=frozenset({"trace"}),
        optional_inputs=frozenset(),
        accepted_contracts=frozenset({"workspace-agent-v1"}),
        default_timeout_seconds=1800,
        resource_profile="network_service",
        stable_error_codes=frozenset(
            {"capacity_exceeded", "engine_timeout", "engine_unavailable"}
        ),
    )

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        return EngineRunRef("smartperfetto", "session-1", "run-1", None)

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> tuple[EngineEvent, ...]:
        return (
            EngineEvent("event-1", "running", 25, "trace_indexed", datetime.now(UTC)),
        )

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult:
        return EngineResult("workspace-agent-v1", "completed", {"report": {}})

    async def cancel(self, run_ref: EngineRunRef) -> str:
        return "canceled"


def test_registry_returns_only_registered_adapter() -> None:
    adapter = FakeAdapter()
    registry = AdapterRegistry((adapter,))

    assert registry.require("smartperfetto") is adapter
    with pytest.raises(AdapterRegistryError, match="not registered"):
        registry.require("android_memory")


def test_registry_rejects_duplicate_engine_ids() -> None:
    with pytest.raises(AdapterRegistryError, match="duplicate"):
        AdapterRegistry((FakeAdapter(), FakeAdapter()))


def test_engine_input_carries_only_ephemeral_location_and_public_metadata() -> None:
    value = EngineInput(
        artifact_id=UUID("40000000-0000-4000-8000-000000000001"),
        kind="trace",
        mime="application/octet-stream",
        size_bytes=1024,
        sha256_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        download_url=SecretStr("https://claim.internal/artifacts/opaque"),
    )

    assert value.kind == "trace"
    assert "bucket" not in type(value).__dataclass_fields__
    assert "object_key" not in type(value).__dataclass_fields__
    assert "claim.internal" not in repr(value)


def test_contract_values_are_frozen() -> None:
    descriptor = FakeAdapter.descriptor

    with pytest.raises(AttributeError):
        descriptor.engine_id = "android_memory"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_fake_adapter_implements_callable_async_protocol_shape() -> None:
    adapter = FakeAdapter()
    config = SubmitConfig(
        analysis_id=UUID("40000000-0000-4000-8000-000000000002"),
        profile="auto",
        question=None,
        external_workspace_id=None,
        timeout_seconds=1800,
    )
    input_value = EngineInput(
        artifact_id=UUID("40000000-0000-4000-8000-000000000003"),
        kind="trace",
        mime="application/octet-stream",
        size_bytes=1024,
        sha256_b64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        download_url=SecretStr("https://claims.example/artifacts/opaque"),
    )

    run_ref = await adapter.submit((input_value,), config)

    assert await adapter.stream(run_ref, run_ref.cursor)
    assert (await adapter.fetch_result(run_ref)).state == "completed"
    assert await adapter.cancel(run_ref) == "canceled"
