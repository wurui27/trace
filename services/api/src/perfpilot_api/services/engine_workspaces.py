"""Race-safe, server-owned SmartPerfetto workspace provisioning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.db.control.models import TeamEngineWorkspace
from perfpilot_api.engines.errors import EngineAdapterError
from perfpilot_api.engines.smartperfetto_contracts import (
    SmartPerfettoWorkspaceCreateResponse,
    SmartPerfettoWorkspaceListResponse,
)
from perfpilot_api.engines.smartperfetto_transport import SmartPerfettoTransport


WorkspaceState = Literal["provisioning", "active", "deleting", "deleted", "failed"]
_ENGINE_ID = "smartperfetto"
_WORKSPACE_NAMESPACE = UUID("fa1ac87c-4825-5a9c-a32f-bc2cc2648bd7")
_STABLE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_WORKSPACE_PATH = "/api/tenant/workspaces"
_WORKSPACE_NAME = "PerfPilot managed workspace"
_QUOTA_POLICY = {
    "maxTraceBytes": 536_870_912,
    "maxConcurrentRuns": 2,
}
_RETENTION_POLICY = {
    "traceRetentionDays": 7,
    "reportRetentionDays": 30,
}


class EngineWorkspaceNotFoundError(RuntimeError):
    """The scoped team/engine mapping does not exist."""


class StaleEngineWorkspaceVersionError(RuntimeError):
    """The workspace mapping changed after it was claimed."""


@dataclass(frozen=True, slots=True)
class EngineWorkspaceRecord:
    id: UUID
    team_id: UUID
    engine_id: str
    external_workspace_id: str | None
    state: WorkspaceState
    version: int


@dataclass(frozen=True, slots=True)
class EngineWorkspaceClaim:
    record: EngineWorkspaceRecord
    is_owner: bool


class EngineWorkspaceRepository(Protocol):
    async def claim(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceClaim: ...

    async def get(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceRecord: ...

    async def activate(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        external_workspace_id: str,
    ) -> EngineWorkspaceRecord: ...

    async def fail(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        stable_error_code: str,
    ) -> EngineWorkspaceRecord: ...


class SQLAlchemyEngineWorkspaceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _record(row: TeamEngineWorkspace) -> EngineWorkspaceRecord:
        return EngineWorkspaceRecord(
            id=row.id,
            team_id=row.team_id,
            engine_id=row.engine_id,
            external_workspace_id=row.external_workspace_id,
            state=row.state,  # type: ignore[arg-type]
            version=row.version,
        )

    @staticmethod
    async def _scoped_row(
        session: AsyncSession,
        *,
        team_id: UUID,
        engine_id: str,
    ) -> TeamEngineWorkspace:
        row = await session.scalar(
            select(TeamEngineWorkspace).where(
                TeamEngineWorkspace.team_id == team_id,
                TeamEngineWorkspace.engine_id == engine_id,
            )
        )
        if row is None:
            raise EngineWorkspaceNotFoundError("engine workspace was not found")
        return row

    async def claim(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceClaim:
        async with self._session_factory.begin() as session:
            created_id = await session.scalar(
                postgresql_insert(TeamEngineWorkspace)
                .values(
                    team_id=team_id,
                    engine_id=engine_id,
                    external_workspace_id=None,
                    state="provisioning",
                    version=1,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        TeamEngineWorkspace.team_id,
                        TeamEngineWorkspace.engine_id,
                    )
                )
                .returning(TeamEngineWorkspace.id)
            )
            row = await self._scoped_row(
                session,
                team_id=team_id,
                engine_id=engine_id,
            )
            return EngineWorkspaceClaim(
                record=self._record(row),
                is_owner=created_id is not None,
            )

    async def get(self, *, team_id: UUID, engine_id: str) -> EngineWorkspaceRecord:
        async with self._session_factory() as session:
            row = await self._scoped_row(
                session,
                team_id=team_id,
                engine_id=engine_id,
            )
            return self._record(row)

    async def activate(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        external_workspace_id: str,
    ) -> EngineWorkspaceRecord:
        async with self._session_factory.begin() as session:
            row = await session.scalar(
                update(TeamEngineWorkspace)
                .where(
                    TeamEngineWorkspace.team_id == team_id,
                    TeamEngineWorkspace.engine_id == engine_id,
                    TeamEngineWorkspace.state == "provisioning",
                    TeamEngineWorkspace.version == expected_version,
                )
                .values(
                    external_workspace_id=external_workspace_id,
                    state="active",
                    version=TeamEngineWorkspace.version + 1,
                    updated_at=func.now(),
                )
                .returning(TeamEngineWorkspace)
            )
            if row is None:
                await self._scoped_row(session, team_id=team_id, engine_id=engine_id)
                raise StaleEngineWorkspaceVersionError(
                    "engine workspace version is stale"
                )
            return self._record(row)

    async def fail(
        self,
        *,
        team_id: UUID,
        engine_id: str,
        expected_version: int,
        stable_error_code: str,
    ) -> EngineWorkspaceRecord:
        if _STABLE_ERROR_CODE.fullmatch(stable_error_code) is None:
            raise ValueError("stable engine error code is invalid")
        async with self._session_factory.begin() as session:
            row = await session.scalar(
                update(TeamEngineWorkspace)
                .where(
                    TeamEngineWorkspace.team_id == team_id,
                    TeamEngineWorkspace.engine_id == engine_id,
                    TeamEngineWorkspace.state == "provisioning",
                    TeamEngineWorkspace.version == expected_version,
                )
                .values(
                    state="failed",
                    version=TeamEngineWorkspace.version + 1,
                    updated_at=func.now(),
                )
                .returning(TeamEngineWorkspace)
            )
            if row is None:
                await self._scoped_row(session, team_id=team_id, engine_id=engine_id)
                raise StaleEngineWorkspaceVersionError(
                    "engine workspace version is stale"
                )
            return self._record(row)


def workspace_candidate_id(team_id: UUID) -> str:
    return f"pp-{uuid5(_WORKSPACE_NAMESPACE, str(team_id))}"


def _adapter_error(stable_code: str, *, retryable: bool) -> EngineAdapterError:
    return EngineAdapterError(
        stable_code=stable_code,
        retryable=retryable,
        terminal_state=None if retryable else "failed",
    )


class EngineWorkspaceService:
    def __init__(
        self,
        repository: EngineWorkspaceRepository,
        transport: SmartPerfettoTransport,
    ) -> None:
        self._repository = repository
        self._transport = transport

    async def ensure_workspace(self, *, team_id: UUID) -> EngineWorkspaceRecord:
        claim = await self._repository.claim(team_id=team_id, engine_id=_ENGINE_ID)
        if claim.record.state == "active" and claim.record.external_workspace_id:
            return claim.record
        if not claim.is_owner:
            raise _adapter_error("engine_unavailable", retryable=True)

        candidate = workspace_candidate_id(team_id)
        try:
            existing = await self._find_candidate(candidate)
        except EngineAdapterError as error:
            await self._record_failure(claim.record, error.stable_code)
            raise
        if existing is not None:
            return await self._activate(claim.record, existing)

        create_error: EngineAdapterError | None = None
        try:
            response = await self._transport.request_json(
                "POST",
                _WORKSPACE_PATH,
                json_body={
                    "workspaceId": candidate,
                    "name": _WORKSPACE_NAME,
                    "quotaPolicy": dict(_QUOTA_POLICY),
                    "retentionPolicy": dict(_RETENTION_POLICY),
                },
            )
            if response.status_code not in {200, 201}:
                raise _adapter_error("engine_unavailable", retryable=True)
            created = SmartPerfettoWorkspaceCreateResponse.model_validate(
                response.payload
            ).workspace.id
            if created != candidate:
                raise _adapter_error("engine_contract_invalid", retryable=False)
            return await self._activate(claim.record, created)
        except ValidationError:
            create_error = _adapter_error("engine_contract_invalid", retryable=False)
        except EngineAdapterError as error:
            create_error = error

        try:
            reconciled = await self._find_candidate(candidate)
        except EngineAdapterError:
            reconciled = None
        if reconciled is not None:
            return await self._activate(claim.record, reconciled)

        assert create_error is not None
        await self._record_failure(claim.record, create_error.stable_code)
        raise create_error

    async def _find_candidate(self, candidate: str) -> str | None:
        response = await self._transport.request_json("GET", _WORKSPACE_PATH)
        if response.status_code != 200:
            raise _adapter_error("engine_unavailable", retryable=True)
        try:
            listed = SmartPerfettoWorkspaceListResponse.model_validate(response.payload)
        except ValidationError:
            raise _adapter_error("engine_contract_invalid", retryable=False) from None
        return next(
            (workspace.id for workspace in listed.workspaces if workspace.id == candidate),
            None,
        )

    async def _activate(
        self,
        record: EngineWorkspaceRecord,
        external_workspace_id: str,
    ) -> EngineWorkspaceRecord:
        return await self._repository.activate(
            team_id=record.team_id,
            engine_id=record.engine_id,
            expected_version=record.version,
            external_workspace_id=external_workspace_id,
        )

    async def _record_failure(
        self,
        record: EngineWorkspaceRecord,
        stable_error_code: str,
    ) -> None:
        await self._repository.fail(
            team_id=record.team_id,
            engine_id=record.engine_id,
            expected_version=record.version,
            stable_error_code=stable_error_code,
        )


__all__ = [
    "EngineWorkspaceClaim",
    "EngineWorkspaceNotFoundError",
    "EngineWorkspaceRecord",
    "EngineWorkspaceRepository",
    "EngineWorkspaceService",
    "SQLAlchemyEngineWorkspaceRepository",
    "StaleEngineWorkspaceVersionError",
    "workspace_candidate_id",
]
