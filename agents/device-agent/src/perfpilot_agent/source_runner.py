from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from perfpilot_agent.security import VerifiedPatchVerificationTask, VerifiedSourceTask
from perfpilot_agent.source_contracts import (
    SourceContractError,
    validate_source_contract_semantics,
)
from perfpilot_agent.source_registry import SourceRegistryError, SourceWorkspaceRegistry
from perfpilot_agent.source_rules import select_source_context
from perfpilot_agent.source_snapshot import SourceSnapshotError, SourceSnapshotter


class SourceCompletionControl(Protocol):
    async def complete_source_task(
        self,
        *,
        execution_id,
        lease_version: int,
        lease_token: str,
        completion: dict[str, object],
    ) -> object: ...


class SourceTaskRunner:
    def __init__(
        self,
        *,
        control: SourceCompletionControl,
        registry: SourceWorkspaceRegistry,
        cache_root: Path,
        snapshotter: SourceSnapshotter | None = None,
    ) -> None:
        self._control = control
        self._registry = registry
        self._snapshotter = snapshotter or SourceSnapshotter(cache_root=cache_root)

    @staticmethod
    def canonical_completion_bytes(completion: dict[str, object]) -> bytes:
        try:
            return json.dumps(
                completion,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise SourceContractError from None

    @staticmethod
    def _base(task: VerifiedSourceTask) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "task_type": task.task_type,
            "execution_id": str(task.execution_id),
            "analysis_id": str(task.analysis_id),
            "workspace_id": str(task.workspace_id),
            "lease_version": task.lease_version,
        }

    def _workspace_path(self, task: VerifiedSourceTask) -> Path:
        matches = tuple(
            workspace
            for workspace in self._registry.list()
            if workspace.workspace_id == task.workspace_id
        )
        if len(matches) != 1 or task.snapshot_policy != "tracked_worktree":
            raise SourceRegistryError
        return matches[0].path

    async def execute_source_context(
        self,
        task: VerifiedSourceTask,
        *,
        lease_token: str,
    ) -> None:
        completion = self._base(task)
        snapshot = None
        try:
            if task.task_type != "source_context":
                raise SourceRegistryError
            workspace_path = self._workspace_path(task)
            snapshot = self._snapshotter.create(
                workspace_path,
                task.workspace_id,
                created_at=task.expires_at,
            )
            context = select_source_context(
                snapshot,
                task.finding_hints,
                max_files=task.limits.max_files,
                max_bytes=task.limits.max_bytes,
            )
            completion.update(
                {
                    "state": "completed",
                    "result": {
                        "snapshot_id": str(snapshot.snapshot_id),
                        "snapshot_hash": snapshot.snapshot_hash,
                        "git_head": snapshot.git_head,
                        "tracked_dirty_count": snapshot.tracked_dirty_count,
                        "fragments": context.fragment_documents(),
                        "exclusions": [item.document() for item in context.exclusions],
                        "truncated": context.truncated,
                    },
                }
            )
            validate_source_contract_semantics(
                "agents/source-task-completion.schema.json",
                completion,
            )
            if len(self.canonical_completion_bytes(completion)) > 127 * 1024:
                raise SourceContractError
        except SourceRegistryError:
            completion.update(
                {
                    "state": "failed",
                    "result": {
                        "failure_code": "source_workspace_unavailable",
                        "retryable": False,
                    },
                }
            )
        except SourceSnapshotError:
            completion.update(
                {
                    "state": "failed",
                    "result": {
                        "failure_code": "source_snapshot_failed",
                        "retryable": False,
                    },
                }
            )
        except (SourceContractError, UnicodeError, ValueError):
            completion.update(
                {
                    "state": "failed",
                    "result": {
                        "failure_code": "source_context_invalid",
                        "retryable": False,
                    },
                }
            )
        await self._control.complete_source_task(
            execution_id=task.execution_id,
            lease_version=task.lease_version,
            lease_token=lease_token,
            completion=completion,
        )
        if snapshot is not None:
            try:
                self._snapshotter.mark_terminal(snapshot.snapshot_id)
                self._snapshotter.cleanup()
            except SourceSnapshotError:
                pass

    async def _execute_patch_shell(
        self,
        task: VerifiedPatchVerificationTask,
        *,
        lease_token: str,
    ) -> None:
        completion = self._base(task)
        completion.update(
            {
                "state": "failed",
                "result": {
                    "verification_state": "unavailable",
                    "exit_code": None,
                    "duration_ms": None,
                    "profile_id": str(task.validation_profile_id),
                    "patch_sha256": hashlib.sha256(task.patch.encode("utf-8")).hexdigest(),
                    "log_summary": None,
                },
            }
        )
        await self._control.complete_source_task(
            execution_id=task.execution_id,
            lease_version=task.lease_version,
            lease_token=lease_token,
            completion=completion,
        )

    async def run(self, task: VerifiedSourceTask, *, lease_token: str) -> None:
        if isinstance(task, VerifiedPatchVerificationTask):
            await self._execute_patch_shell(task, lease_token=lease_token)
            return
        await self.execute_source_context(task, lease_token=lease_token)


__all__ = ["SourceTaskRunner"]
