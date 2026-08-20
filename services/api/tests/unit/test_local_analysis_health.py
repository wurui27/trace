from __future__ import annotations

from datetime import UTC, datetime, timedelta

from perfpilot_api.local_analysis_health import (
    AnalysisHealth,
    CapabilityHealth,
    HealthAggregator,
    supervisor_capability,
)


NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def capability(
    name: str,
    state: str = "healthy",
    message: str = "可用",
) -> CapabilityHealth:
    return CapabilityHealth(
        name=name,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        message=message,
        last_checked_at=NOW,
    )


def test_smartperfetto_outage_degrades_general_readiness() -> None:
    health = HealthAggregator().readiness(
        (
            capability("smartperfetto", "unavailable", "Trace 分析暂不可用"),
            capability("ai"),
            capability("storage"),
            capability("supervisor"),
        )
    )

    assert health.state == "degraded"
    assert health.for_mode("trace_upload").state == "unavailable"


def test_storage_outage_makes_readiness_unavailable() -> None:
    health = HealthAggregator().readiness(
        (
            capability("smartperfetto"),
            capability("ai"),
            capability("storage", "unavailable", "存储不可写"),
            capability("supervisor"),
        )
    )

    assert health.state == "unavailable"


def test_offline_agent_only_blocks_device_and_source_modes() -> None:
    health = HealthAggregator().readiness(
        (
            capability("smartperfetto"),
            capability("ai"),
            capability("agent", "unavailable", "没有在线 Agent"),
            capability("device", "unavailable", "没有可用设备"),
            capability("source", "unavailable", "没有可读源码工作区"),
            capability("storage"),
            capability("supervisor"),
        )
    )

    assert health.state == "degraded"
    assert health.for_mode("trace_upload").state == "healthy"
    assert health.for_mode("device").state == "unavailable"
    assert health.for_mode("trace_upload", source_requested=True).state == "unavailable"


def test_stale_supervisor_is_unavailable() -> None:
    capability_health = supervisor_capability(
        last_tick_at=NOW - timedelta(seconds=31),
        now=NOW,
        stale_after_seconds=30,
    )

    assert capability_health.name == "supervisor"
    assert capability_health.state == "unavailable"


def test_health_document_is_closed_and_safe() -> None:
    document = AnalysisHealth(
        state="healthy",
        capabilities=(capability("storage"), capability("supervisor")),
    ).document()

    assert set(document) == {"schema_version", "state", "capabilities"}
    assert set(document["capabilities"][0]) == {
        "name",
        "state",
        "message",
        "last_checked_at",
    }
