from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from perfpilot_api.engines.contracts import (
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineStatus,
    SubmitConfig,
)
from perfpilot_api.local_memory_analysis import LocalAndroidMemoryAnalysisGateway


class _FakeMemoryAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.inputs: tuple[EngineInput, ...] = ()
        self.config: SubmitConfig | None = None
        self.downloaded: dict[str, bytes] = {}
        self.status_calls = 0
        self.cancel_calls = 0

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        self.inputs = inputs
        self.config = config
        for item in inputs:
            response = await self.client.get(item.download_url.get_secret_value())
            response.raise_for_status()
            self.downloaded[item.kind] = response.content
        return EngineRunRef(
            engine_id="android_memory",
            external_session_id=None,
            external_run_id=f"memory-{config.execution_id.hex}",
            cursor=None,
        )

    async def status(self, run_ref: EngineRunRef) -> EngineStatus:
        self.status_calls += 1
        return EngineStatus(
            run_ref=run_ref,
            state="running" if self.status_calls == 1 else "completed",
            stable_error_code=None,
            retryable=False,
        )

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult:
        return EngineResult(
            contract="android-memory-ai-context-1.2",
            state="completed",
            payload={
                "context_type": "android-memory-ai-context",
                "schema_version": "1.2",
                "generator": {"name": "android-memory-ai", "version": "1.2.0"},
                "analysis_contract": {
                    "support_level": "limited",
                    "primary_intent_support_level": "limited",
                    "privacy": {
                        "raw_contents_embedded": False,
                        "local_paths_included": False,
                    },
                },
            },
        )

    async def cancel(self, run_ref: EngineRunRef) -> str:
        self.cancel_calls += 1
        return "canceled"


@pytest.mark.asyncio
async def test_local_memory_gateway_serves_opaque_verified_inputs_and_returns_result(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "memory-evidence.tar"
    archive.write_bytes(b"local-memory-evidence")
    adapters: list[_FakeMemoryAdapter] = []
    shutdown_calls = 0

    def adapter_factory(client: httpx.AsyncClient) -> _FakeMemoryAdapter:
        adapter = _FakeMemoryAdapter(client)
        adapters.append(adapter)
        return adapter

    async def shutdown() -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    gateway = LocalAndroidMemoryAnalysisGateway(
        adapter_factory=adapter_factory,
        shutdown=shutdown,
        engine_commit_sha="d5514972ced78c3faa7fc17589c1ea9231645056",
        poll_interval_seconds=0,
    )
    analysis_id = UUID("82000000-0000-4000-8000-000000000001")

    result = await gateway.analyze(
        analysis_id=analysis_id,
        evidence_path=archive,
        package_name="com.example.perfpilot",
        android_release="13",
        api_level=33,
    )
    await gateway.aclose()

    assert result.state == "completed"
    assert gateway.engine_commit_sha == "d5514972ced78c3faa7fc17589c1ea9231645056"
    adapter = adapters[0]
    assert adapter.config is not None
    assert adapter.config.analysis_id == analysis_id
    assert adapter.config.profile == "auto"
    assert adapter.config.question is None
    assert adapter.config.external_workspace_id is None
    assert adapter.status_calls == 2
    assert adapter.downloaded["memory_evidence"] == b"local-memory-evidence"
    manifest = json.loads(adapter.downloaded["memory_capture_manifest"])
    assert manifest["analysis_id"] == str(analysis_id)
    assert manifest["source"] == "adb_agent"
    assert manifest["phase"] == "single"
    assert manifest["subject"] == {
        "package": "com.example.perfpilot",
        "android_release": "13",
        "android_sdk": 33,
    }
    assert manifest["artifacts"] == [
        {
            "artifact_id": str(adapter.inputs[1].artifact_id),
            "role": "handoff_archive",
        }
    ]
    serialized_inputs = " ".join(
        item.download_url.get_secret_value() for item in adapter.inputs
    )
    assert str(tmp_path) not in serialized_inputs
    assert "memory-evidence.tar" not in serialized_inputs
    assert shutdown_calls == 1
