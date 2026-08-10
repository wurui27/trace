from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
import unicodedata
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SourceBinding:
    provider_kind: Literal["agent_workspace"]
    agent_id: UUID
    workspace_id: UUID
    snapshot_policy: Literal["tracked_worktree"]
    validation_profile_id: UUID | None


@dataclass(frozen=True, slots=True)
class SourceValidationProfileView:
    profile_id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class SourceWorkspaceView:
    provider_kind: Literal["agent_workspace"]
    agent_id: UUID
    agent_name: str
    workspace_id: UUID
    name: str
    state: Literal["ready", "invalid"]
    git_branch: str | None
    git_head: str
    tracked_dirty_count: int
    snapshot_policy: Literal["tracked_worktree"]
    validation_profiles: tuple[SourceValidationProfileView, ...]


@dataclass(frozen=True, slots=True)
class SourceAgentCapabilityRecord:
    agent_id: UUID
    team_id: UUID
    name: str
    state: str
    capabilities: dict[str, object]


class SourceWorkspaceRepository(Protocol):
    async def list_source_agents(
        self, team_id: UUID
    ) -> tuple[SourceAgentCapabilityRecord, ...]: ...

    async def get_source_agent(
        self, agent_id: UUID
    ) -> SourceAgentCapabilityRecord | None: ...


class SourceWorkspaceError(RuntimeError):
    pass


class SourceCodeAnalysisDisabled(SourceWorkspaceError):
    def __init__(self) -> None:
        super().__init__("source_code_analysis_disabled")


class SourceBindingNotFound(SourceWorkspaceError):
    pass


class SourceBindingInvalid(SourceWorkspaceError):
    pass


class SourceWorkspaceService:
    def __init__(
        self,
        *,
        repository: SourceWorkspaceRepository | None,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._enabled = enabled

    async def list_for_team(self, *, team_id: UUID) -> tuple[SourceWorkspaceView, ...]:
        if not self._enabled:
            return ()
        if self._repository is None:
            raise SourceBindingInvalid("source workspace service is unavailable")
        result: list[SourceWorkspaceView] = []
        for agent in await self._repository.list_source_agents(team_id):
            if agent.team_id != team_id or agent.state != "online":
                continue
            for workspace in _parse_capabilities(agent):
                if workspace.state == "ready":
                    result.append(workspace)
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.agent_name.casefold(),
                    item.name.casefold(),
                    str(item.workspace_id),
                ),
            )
        )

    async def require_binding(
        self,
        *,
        team_id: UUID,
        binding: SourceBinding,
    ) -> SourceBinding:
        if not self._enabled:
            raise SourceCodeAnalysisDisabled
        if self._repository is None:
            raise SourceBindingInvalid("source workspace service is unavailable")
        if (
            binding.provider_kind != "agent_workspace"
            or binding.snapshot_policy != "tracked_worktree"
            or binding.agent_id.version not in range(1, 6)
            or binding.workspace_id.version not in range(1, 6)
            or (
                binding.validation_profile_id is not None
                and binding.validation_profile_id.version not in range(1, 6)
            )
        ):
            raise SourceBindingInvalid("source binding is invalid")
        agent = await self._repository.get_source_agent(binding.agent_id)
        if agent is None or agent.team_id != team_id:
            raise SourceBindingNotFound("source workspace was not found")
        if agent.state != "online":
            raise SourceBindingInvalid("source workspace is unavailable")
        workspace = next(
            (
                item
                for item in _parse_capabilities(agent)
                if item.workspace_id == binding.workspace_id
            ),
            None,
        )
        if workspace is None:
            raise SourceBindingNotFound("source workspace was not found")
        if workspace.state != "ready" or workspace.snapshot_policy != binding.snapshot_policy:
            raise SourceBindingInvalid("source workspace is unavailable")
        if binding.validation_profile_id is not None and binding.validation_profile_id not in {
            profile.profile_id for profile in workspace.validation_profiles
        }:
            raise SourceBindingInvalid("source validation profile is invalid")
        return binding


def is_public_source_display_name(value: str) -> bool:
    candidate = value.lstrip()
    return not (
        candidate.startswith(("/", "\\", "~/", "~\\", "./", ".\\", "../", "..\\"))
        or candidate.casefold().startswith("file:")
        or (
            len(candidate) >= 3
            and candidate[0].isascii()
            and candidate[0].isalpha()
            and candidate[1] == ":"
            and candidate[2] in {"/", "\\"}
        )
    )


def _parse_capabilities(
    agent: SourceAgentCapabilityRecord,
) -> tuple[SourceWorkspaceView, ...]:
    raw_workspaces = agent.capabilities.get("source_workspaces")
    if not isinstance(raw_workspaces, list) or len(raw_workspaces) > 32:
        return ()
    parsed: list[SourceWorkspaceView] = []
    workspace_ids: set[UUID] = set()
    try:
        for raw in raw_workspaces:
            if not isinstance(raw, dict) or set(raw) != {
                "workspace_id",
                "name",
                "state",
                "git_branch",
                "git_head",
                "tracked_dirty_count",
                "snapshot_policy",
                "validation_profiles",
            }:
                raise ValueError
            workspace_id = UUID(str(raw["workspace_id"]))
            name = raw["name"]
            state = raw["state"]
            branch = raw["git_branch"]
            head = raw["git_head"]
            dirty = raw["tracked_dirty_count"]
            profiles = raw["validation_profiles"]
            if (
                workspace_id in workspace_ids
                or str(workspace_id) != raw["workspace_id"]
                or workspace_id.version not in range(1, 6)
                or not isinstance(name, str)
                or not 1 <= len(name) <= 128
                or any(unicodedata.category(character) == "Cc" for character in name)
                or not is_public_source_display_name(name)
                or state not in {"ready", "invalid"}
                or (branch is not None and (not isinstance(branch, str) or not 1 <= len(branch) <= 255))
                or (
                    isinstance(branch, str)
                    and any(unicodedata.category(character) == "Cc" for character in branch)
                )
                or not isinstance(head, str)
                or len(head) != 40
                or any(character not in "0123456789abcdef" for character in head)
                or type(dirty) is not int
                or dirty < 0
                or raw["snapshot_policy"] != "tracked_worktree"
                or not isinstance(profiles, list)
                or len(profiles) > 8
            ):
                raise ValueError
            parsed_profiles: list[SourceValidationProfileView] = []
            profile_ids: set[UUID] = set()
            for profile in profiles:
                if not isinstance(profile, dict) or set(profile) != {"profile_id", "name"}:
                    raise ValueError
                profile_id = UUID(str(profile["profile_id"]))
                profile_name = profile["name"]
                if (
                    profile_id in profile_ids
                    or str(profile_id) != profile["profile_id"]
                    or profile_id.version not in range(1, 6)
                    or not isinstance(profile_name, str)
                    or not 1 <= len(profile_name) <= 128
                    or not is_public_source_display_name(profile_name)
                    or any(
                        unicodedata.category(character) == "Cc"
                        for character in profile_name
                    )
                ):
                    raise ValueError
                profile_ids.add(profile_id)
                parsed_profiles.append(
                    SourceValidationProfileView(profile_id=profile_id, name=profile_name)
                )
            workspace_ids.add(workspace_id)
            parsed.append(
                SourceWorkspaceView(
                    provider_kind="agent_workspace",
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    workspace_id=workspace_id,
                    name=name,
                    state=state,  # type: ignore[arg-type]
                    git_branch=branch,
                    git_head=head,
                    tracked_dirty_count=dirty,
                    snapshot_policy="tracked_worktree",
                    validation_profiles=tuple(parsed_profiles),
                )
            )
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(parsed)


__all__ = [
    "SourceAgentCapabilityRecord",
    "SourceBinding",
    "SourceBindingInvalid",
    "SourceBindingNotFound",
    "SourceCodeAnalysisDisabled",
    "SourceValidationProfileView",
    "SourceWorkspaceRepository",
    "SourceWorkspaceService",
    "SourceWorkspaceView",
    "is_public_source_display_name",
]
