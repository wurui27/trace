"""Explicit registry for the adapters enabled in this process."""

from __future__ import annotations

from collections.abc import Iterable

from perfpilot_api.engines.contracts import EngineAdapter


class AdapterRegistryError(RuntimeError):
    """The configured external engine adapters are invalid or unavailable."""


class AdapterRegistry:
    """Maps a unique engine identifier to its explicitly registered adapter."""

    def __init__(self, adapters: Iterable[EngineAdapter]) -> None:
        self._adapters: dict[str, EngineAdapter] = {}
        for adapter in adapters:
            engine_id = adapter.descriptor.engine_id
            if engine_id in self._adapters:
                raise AdapterRegistryError(f"duplicate engine adapter: {engine_id}")
            self._adapters[engine_id] = adapter

    def require(self, engine_id: str) -> EngineAdapter:
        """Return a registered adapter; never substitute an implicit fallback."""

        adapter = self._adapters.get(engine_id)
        if adapter is None:
            raise AdapterRegistryError(f"engine adapter is not registered: {engine_id}")
        return adapter
