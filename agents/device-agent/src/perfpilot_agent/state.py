from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


class RuntimeStateError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PerfPilot Agent runtime state is inconsistent")


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    client_ref: UUID
    device_id: UUID
    device_digest: str
    serial: str = field(repr=False)


class AgentRuntimeState:
    def __init__(self) -> None:
        self._serial_by_digest: dict[str, str] = {}
        self._device_id_by_digest: dict[str, UUID] = {}
        self._execution_id: UUID | None = None
        self._clock_skew_ms = 0

    def __repr__(self) -> str:
        execution = "idle" if self._execution_id is None else "busy"
        return f"AgentRuntimeState(devices={len(self._serial_by_digest)}, execution={execution!r})"

    @property
    def execution_id(self) -> UUID | None:
        return self._execution_id

    @property
    def clock_skew_ms(self) -> int:
        return self._clock_skew_ms

    def set_execution(self, execution_id: UUID | None) -> None:
        self._execution_id = execution_id

    def set_clock_skew(self, milliseconds: int) -> None:
        if isinstance(milliseconds, bool) or not -300_000 <= milliseconds <= 300_000:
            raise RuntimeStateError
        self._clock_skew_ms = milliseconds

    def replace_device_bindings(self, bindings: tuple[DeviceBinding, ...]) -> None:
        digests = {binding.device_digest for binding in bindings}
        serials = {binding.serial for binding in bindings}
        device_ids = {binding.device_id for binding in bindings}
        if (
            len(digests) != len(bindings)
            or len(serials) != len(bindings)
            or len(device_ids) != len(bindings)
            or any(len(digest) != 64 for digest in digests)
        ):
            raise RuntimeStateError
        self._serial_by_digest = {binding.device_digest: binding.serial for binding in bindings}
        self._device_id_by_digest = {
            binding.device_digest: binding.device_id for binding in bindings
        }

    def serial_for_digest(self, device_digest: str) -> str | None:
        return self._serial_by_digest.get(device_digest)

    def device_id_for_digest(self, device_digest: str) -> UUID | None:
        return self._device_id_by_digest.get(device_digest)

    def known_device_digests(self) -> frozenset[str]:
        return frozenset(self._serial_by_digest)


__all__ = ["AgentRuntimeState", "DeviceBinding", "RuntimeStateError"]
