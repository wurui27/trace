from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

SecretPurpose = Literal[
    "tenant_database_password",
    "tenant_database_migration_password",
]
_SECRET_PURPOSES = frozenset({"tenant_database_password", "tenant_database_migration_password"})


class SecretStoreError(RuntimeError):
    """A redacted failure from a secret-store operation."""


class SecretNotFoundError(SecretStoreError):
    """The opaque reference does not identify a stored secret."""


@dataclass(frozen=True, slots=True)
class SecretContext:
    team_id: UUID
    resource_id: UUID
    credential_version: int
    purpose: SecretPurpose

    def __post_init__(self) -> None:
        if self.credential_version < 1:
            raise ValueError("credential_version must be positive")
        if self.purpose not in _SECRET_PURPOSES:
            raise ValueError("unsupported secret purpose")


class SecretStore(Protocol):
    def allocate_reference(self) -> str: ...

    async def put(
        self,
        secret: bytes,
        *,
        context: SecretContext,
        reference: str | None = None,
    ) -> str: ...

    async def get(self, reference: str, *, context: SecretContext) -> bytes: ...

    async def delete(self, reference: str) -> None: ...
