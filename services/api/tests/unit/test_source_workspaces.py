from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest

from perfpilot_api.services.source_workspaces import (
    SourceBinding,
    SourceBindingInvalid,
    SourceBindingNotFound,
    SourceCodeAnalysisDisabled,
    SourceWorkspaceService,
)


TEAM_ID = UUID("81000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("81000000-0000-4000-8000-000000000002")
AGENT_ID = UUID("91000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
PROFILE_ID = UUID("94000000-0000-4000-8000-000000000001")


def _capability(*, state: str = "ready") -> dict[str, object]:
    return {
        "workspace_id": str(WORKSPACE_ID),
        "name": "Demo Android",
        "state": state,
        "git_branch": "main",
        "git_head": "1" * 40,
        "tracked_dirty_count": 1,
        "snapshot_policy": "tracked_worktree",
        "validation_profiles": [
            {"profile_id": str(PROFILE_ID), "name": "Android check"}
        ],
    }


@dataclass(frozen=True, slots=True)
class _Agent:
    agent_id: UUID
    team_id: UUID
    name: str
    state: str
    capabilities: dict[str, object]


class _Repository:
    def __init__(self, *agents: _Agent) -> None:
        self.agents = {agent.agent_id: agent for agent in agents}

    async def list_source_agents(self, team_id: UUID) -> tuple[_Agent, ...]:
        return tuple(agent for agent in self.agents.values() if agent.team_id == team_id)

    async def get_source_agent(self, agent_id: UUID) -> _Agent | None:
        return self.agents.get(agent_id)


def _agent(
    *,
    team_id: UUID = TEAM_ID,
    state: str = "online",
    workspace_state: str = "ready",
    capabilities: dict[str, object] | None = None,
) -> _Agent:
    return _Agent(
        agent_id=AGENT_ID,
        team_id=team_id,
        name="Ray Mac",
        state=state,
        capabilities=(
            {"source_workspaces": [_capability(state=workspace_state)]}
            if capabilities is None
            else capabilities
        ),
    )


def _binding(*, profile_id: UUID | None = PROFILE_ID) -> SourceBinding:
    return SourceBinding(
        provider_kind="agent_workspace",
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        snapshot_policy="tracked_worktree",
        validation_profile_id=profile_id,
    )


@pytest.mark.asyncio
async def test_directory_returns_only_online_ready_current_team_workspaces() -> None:
    repository = _Repository(
        _agent(),
        _Agent(
            agent_id=UUID("91000000-0000-4000-8000-000000000002"),
            team_id=TEAM_ID,
            name="Offline",
            state="offline",
            capabilities={"source_workspaces": [_capability()]},
        ),
        _Agent(
            agent_id=UUID("91000000-0000-4000-8000-000000000003"),
            team_id=OTHER_TEAM_ID,
            name="Other team",
            state="online",
            capabilities={"source_workspaces": [_capability()]},
        ),
    )
    service = SourceWorkspaceService(repository=repository, enabled=True)  # type: ignore[arg-type]

    workspaces = await service.list_for_team(team_id=TEAM_ID)

    assert len(workspaces) == 1
    assert workspaces[0].agent_id == AGENT_ID
    assert workspaces[0].workspace_id == WORKSPACE_ID
    assert workspaces[0].validation_profiles[0].profile_id == PROFILE_ID


@pytest.mark.asyncio
async def test_directory_skips_invalid_or_malformed_capabilities_without_leaking_values() -> None:
    private_path = "/Users/ray/private/demo"
    repository = _Repository(
        _agent(workspace_state="invalid"),
        _Agent(
            agent_id=UUID("91000000-0000-4000-8000-000000000004"),
            team_id=TEAM_ID,
            name="Malformed",
            state="online",
            capabilities={
                "source_workspaces": [{**_capability(), "path": private_path}]
            },
        ),
    )
    service = SourceWorkspaceService(repository=repository, enabled=True)  # type: ignore[arg-type]

    assert await service.list_for_team(team_id=TEAM_ID) == ()


@pytest.mark.asyncio
async def test_binding_is_reread_and_returns_only_identifiers() -> None:
    repository = _Repository(_agent())
    service = SourceWorkspaceService(repository=repository, enabled=True)  # type: ignore[arg-type]

    validated = await service.require_binding(team_id=TEAM_ID, binding=_binding())

    assert validated == _binding()
    assert not hasattr(validated, "path")
    repository.agents[AGENT_ID] = _agent(state="offline")
    with pytest.raises(SourceBindingInvalid):
        await service.require_binding(team_id=TEAM_ID, binding=_binding())


@pytest.mark.asyncio
async def test_binding_errors_are_distinct_and_disabled_is_stable() -> None:
    enabled = SourceWorkspaceService(repository=_Repository(_agent()), enabled=True)  # type: ignore[arg-type]
    disabled = SourceWorkspaceService(repository=_Repository(_agent()), enabled=False)  # type: ignore[arg-type]

    assert await disabled.list_for_team(team_id=TEAM_ID) == ()
    with pytest.raises(SourceCodeAnalysisDisabled, match="^source_code_analysis_disabled$"):
        await disabled.require_binding(team_id=TEAM_ID, binding=_binding())
    with pytest.raises(SourceBindingNotFound):
        await enabled.require_binding(team_id=OTHER_TEAM_ID, binding=_binding())
    with pytest.raises(SourceBindingInvalid):
        await enabled.require_binding(
            team_id=TEAM_ID,
            binding=_binding(profile_id=UUID("94000000-0000-4000-8000-000000000009")),
        )
