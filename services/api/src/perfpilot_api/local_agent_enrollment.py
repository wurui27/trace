from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4


_ENROLLMENT_LIFETIME = timedelta(minutes=10)


class LocalAgentEnrollmentError(RuntimeError):
    pass


class LocalAgentEnrollmentBusy(LocalAgentEnrollmentError):
    def __init__(self) -> None:
        super().__init__("Agent enrollment is unavailable")


@dataclass(frozen=True, slots=True)
class LocalAgentEnrollment:
    enrollment_id: UUID
    team_id: UUID
    owner_user_id: UUID
    name: str
    created_at: datetime
    expires_at: datetime

    def public_view(self) -> dict[str, object]:
        return {
            "enrollment_id": self.enrollment_id,
            "name": self.name,
            "expires_at": self.expires_at,
        }


class LocalAgentEnrollmentBroker:
    """One bounded, process-local automatic enrollment slot for all teams."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._lock = asyncio.Lock()
        self._enrollment: LocalAgentEnrollment | None = None
        self._state: Literal["open", "claiming"] | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LocalAgentEnrollmentError("Agent enrollment is unavailable")
        return value.astimezone(UTC)

    def _active(self, now: datetime) -> LocalAgentEnrollment | None:
        enrollment = self._enrollment
        if enrollment is not None and enrollment.expires_at <= now:
            self._enrollment = None
            self._state = None
            return None
        return enrollment

    async def open(
        self,
        *,
        team_id: UUID,
        owner_user_id: UUID,
        name: str,
    ) -> LocalAgentEnrollment:
        normalized = name.strip() if isinstance(name, str) else ""
        if (
            not isinstance(team_id, UUID)
            or not isinstance(owner_user_id, UUID)
            or not normalized
            or len(normalized) > 200
            or any(unicodedata.category(character) == "Cc" for character in normalized)
        ):
            raise LocalAgentEnrollmentError("Agent enrollment is unavailable")
        async with self._lock:
            now = self._now()
            active = self._active(now)
            if active is not None:
                if (
                    self._state == "open"
                    and active.team_id == team_id
                    and active.owner_user_id == owner_user_id
                    and active.name == normalized
                ):
                    return active
                raise LocalAgentEnrollmentBusy
            enrollment_id = self._uuid_factory()
            if not isinstance(enrollment_id, UUID) or enrollment_id.version != 4:
                raise LocalAgentEnrollmentError("Agent enrollment is unavailable")
            enrollment = LocalAgentEnrollment(
                enrollment_id=enrollment_id,
                team_id=team_id,
                owner_user_id=owner_user_id,
                name=normalized,
                created_at=now,
                expires_at=now + _ENROLLMENT_LIFETIME,
            )
            self._enrollment = enrollment
            self._state = "open"
            return enrollment

    async def status(self, team_id: UUID) -> dict[str, object] | None:
        async with self._lock:
            active = self._active(self._now())
            if active is None or active.team_id != team_id:
                return None
            return active.public_view()

    async def claim(self) -> LocalAgentEnrollment | None:
        async with self._lock:
            active = self._active(self._now())
            if active is None or self._state != "open":
                return None
            self._state = "claiming"
            return active

    async def release(self, enrollment_id: UUID) -> None:
        async with self._lock:
            active = self._active(self._now())
            if (
                active is not None
                and active.enrollment_id == enrollment_id
                and self._state == "claiming"
            ):
                self._state = "open"

    async def complete(self, enrollment_id: UUID) -> None:
        async with self._lock:
            active = self._active(self._now())
            if active is not None and active.enrollment_id == enrollment_id:
                self._enrollment = None
                self._state = None

    async def cancel(self, *, team_id: UUID, enrollment_id: UUID) -> bool:
        async with self._lock:
            active = self._active(self._now())
            if (
                active is None
                or active.team_id != team_id
                or active.enrollment_id != enrollment_id
                or self._state != "open"
            ):
                return False
            self._enrollment = None
            self._state = None
            return True


__all__ = [
    "LocalAgentEnrollment",
    "LocalAgentEnrollmentBroker",
    "LocalAgentEnrollmentBusy",
    "LocalAgentEnrollmentError",
]
