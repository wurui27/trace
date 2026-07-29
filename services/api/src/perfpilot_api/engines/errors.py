"""Stable, redacted failures raised by external engine adapters."""

from __future__ import annotations

import re
from typing import Literal


EngineErrorTerminalState = Literal["failed", "insufficient_data", "canceled"]
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class EngineAdapterError(RuntimeError):
    """An error boundary that never renders upstream or secret material."""

    __slots__ = ("_retryable", "_stable_code", "_terminal_state")

    def __init__(
        self,
        *,
        stable_code: str,
        retryable: bool,
        terminal_state: EngineErrorTerminalState | None = "failed",
    ) -> None:
        if _STABLE_CODE.fullmatch(stable_code) is None:
            raise ValueError("stable engine error code is invalid")
        if terminal_state not in {None, "failed", "insufficient_data", "canceled"}:
            raise ValueError("engine error terminal state is invalid")
        super().__init__("engine adapter operation failed")
        self._stable_code = stable_code
        self._retryable = retryable
        self._terminal_state = terminal_state

    @property
    def stable_code(self) -> str:
        return self._stable_code

    @property
    def retryable(self) -> bool:
        return self._retryable

    @property
    def terminal_state(self) -> EngineErrorTerminalState | None:
        return self._terminal_state

    def __str__(self) -> str:
        return "engine adapter operation failed"

    def __repr__(self) -> str:
        return "EngineAdapterError(<redacted>)"


__all__ = ["EngineAdapterError", "EngineErrorTerminalState"]
