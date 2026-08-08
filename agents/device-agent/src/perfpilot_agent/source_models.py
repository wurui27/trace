from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    profile_id: UUID
    name: str
    argv: tuple[str, ...] = field(repr=False)
    working_directory: str
    timeout_seconds: int
    allowed_exit_codes: tuple[int, ...]

    def public_document(self) -> dict[str, str]:
        return {"profile_id": str(self.profile_id), "name": self.name}


@dataclass(frozen=True, slots=True)
class SourceWorkspace:
    workspace_id: UUID
    name: str
    path: Path = field(repr=False)
    validation_profiles: tuple[ValidationProfile, ...]

    def public_document(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "name": self.name,
            "validation_profiles": [
                profile.public_document() for profile in self.validation_profiles
            ],
        }


__all__ = ["SourceWorkspace", "ValidationProfile"]
