from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.smartperfetto_transport import SmartPerfettoTransport
from perfpilot_api.services.engine_workspaces import (
    EngineWorkspaceClaim,
    EngineWorkspaceRecord,
    EngineWorkspaceService,
    workspace_candidate_id,
)


TEAM_ID = UUID("b1000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("b1000000-0000-4000-8000-000000000002")
_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "smartperfetto_workspace_agent_v1"
)


class FakeRepository:
    def __init__(self, record: EngineWorkspaceRecord, *, owner: bool = True) -> None:
        self.record = record
        self.owner = owner
        self.activations: list[tuple[UUID, str, int, str]] = []
        self.failures: list[tuple[UUID, str, int, str]] = []

    async def claim(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceClaim:
        assert team_id == self.record.team_id
        assert engine_id == self.record.engine_id
        return EngineWorkspaceClaim(record=self.record, is_owner=self.owner)

    async def get(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceRecord:
        assert team_id == self.record.team_id
        assert engine_id == self.record.engine_id
        return self.record

    async def activate(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        external_workspace_id: str,
    ) -> EngineWorkspaceRecord:
        self.activations.append(
            (team_id, engine_id, expected_version, external_workspace_id)
        )
        self.record = replace(
            self.record,
            external_workspace_id=external_workspace_id,
            state="active",
            version=expected_version + 1,
        )
        return self.record

    async def fail(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        stable_error_code: str,
    ) -> EngineWorkspaceRecord:
        self.failures.append((team_id, engine_id, expected_version, stable_error_code))
        self.record = replace(self.record, state="failed", version=expected_version + 1)
        return self.record


def _record(*, state: str = "provisioning", external_id: str | None = None) -> EngineWorkspaceRecord:
    return EngineWorkspaceRecord(
        id=UUID("b2000000-0000-4000-8000-000000000001"),
        team_id=TEAM_ID,
        engine_id="smartperfetto",
        external_workspace_id=external_id,
        state=state,
        version=1,
    )


async def _credential(_: SecretStr) -> SecretStr:
    return SecretStr("service-secret")


def _transport(
    handler: httpx.MockTransport,
) -> tuple[SmartPerfettoTransport, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler, follow_redirects=False)
    return (
        SmartPerfettoTransport(
            base_url="https://smartperfetto.example.com",
            credential_reference=SecretStr("secret-ref"),
            credential_resolver=_credential,
            client=client,
            max_json_bytes=64 * 1024,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_existing_active_mapping_returns_without_http() -> None:
    repository = FakeRepository(_record(state="active", external_id="pp-existing"), owner=False)

    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("active mapping must not call SmartPerfetto")

    transport, client = _transport(httpx.MockTransport(forbidden))
    try:
        result = await EngineWorkspaceService(repository, transport).ensure_workspace(
            team_id=TEAM_ID
        )
    finally:
        await client.aclose()

    assert result.external_workspace_id == "pp-existing"


@pytest.mark.asyncio
async def test_provisioning_lists_before_create_and_sends_the_frozen_body() -> None:
    repository = FakeRepository(_record())
    requests: list[httpx.Request] = []
    candidate = workspace_candidate_id(TEAM_ID)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "workspaces": []})
        return httpx.Response(
            201,
            json={"success": True, "workspace": {"id": candidate, "name": "ignored"}},
        )

    transport, client = _transport(httpx.MockTransport(handler))
    try:
        result = await EngineWorkspaceService(repository, transport).ensure_workspace(
            team_id=TEAM_ID
        )
    finally:
        await client.aclose()

    expected_body = json.loads(
        (_FIXTURE_ROOT / "workspace-create-request.json").read_text(encoding="utf-8")
    )
    expected_body["workspaceId"] = candidate
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/tenant/workspaces"),
        ("POST", "/api/tenant/workspaces"),
    ]
    assert json.loads(requests[1].content) == expected_body
    assert result.external_workspace_id == candidate


@pytest.mark.asyncio
async def test_exact_candidate_from_list_is_adopted_without_create() -> None:
    repository = FakeRepository(_record())
    candidate = workspace_candidate_id(TEAM_ID)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "workspaces": [
                    {"id": "different-id", "name": "PerfPilot managed workspace"},
                    {"id": candidate, "name": "renamed by operator"},
                ],
            },
        )

    transport, client = _transport(httpx.MockTransport(handler))
    try:
        result = await EngineWorkspaceService(repository, transport).ensure_workspace(
            team_id=TEAM_ID
        )
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert result.external_workspace_id == candidate


@pytest.mark.asyncio
async def test_ambiguous_create_failure_reconciles_once_by_exact_id() -> None:
    repository = FakeRepository(_record())
    candidate = workspace_candidate_id(TEAM_ID)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"success": True, "workspaces": []})
        if len(requests) == 2:
            return httpx.Response(503, text="upstream-secret-marker")
        return httpx.Response(
            200,
            json={"success": True, "workspaces": [{"id": candidate, "name": "managed"}]},
        )

    transport, client = _transport(httpx.MockTransport(handler))
    try:
        result = await EngineWorkspaceService(repository, transport).ensure_workspace(
            team_id=TEAM_ID
        )
    finally:
        await client.aclose()

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert result.external_workspace_id == candidate
    assert repository.failures == []


@pytest.mark.asyncio
async def test_similar_name_never_proves_workspace_ownership() -> None:
    repository = FakeRepository(_record())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "workspaces": [
                        {"id": "attacker-owned-id", "name": "PerfPilot managed workspace"}
                    ],
                },
            )
        return httpx.Response(409, json={"success": False, "code": "WORKSPACE_EXISTS"})

    transport, client = _transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(EngineAdapterError) as exc_info:
            await EngineWorkspaceService(repository, transport).ensure_workspace(team_id=TEAM_ID)
    finally:
        await client.aclose()

    assert exc_info.value.stable_code == "engine_unavailable"
    assert repository.activations == []
    assert repository.failures[-1][-1] == "engine_unavailable"


def test_workspace_identity_and_policy_cannot_come_from_caller() -> None:
    signature = inspect.signature(EngineWorkspaceService.ensure_workspace)

    assert tuple(signature.parameters) == ("self", "team_id")
    assert not hasattr(EngineWorkspaceService, "delete_workspace")
