from __future__ import annotations

import asyncio

import pytest

from perfpilot_api.local_stage_execution import (
    StageExecutionRejected,
    StageResult,
    execute_ai_stage,
    execute_report_stage,
    execute_smartperfetto_stage,
    execute_source_stage,
)


def _guard() -> None:
    return None


@pytest.mark.asyncio
async def test_smartperfetto_partial_failure_preserves_successful_scenario() -> None:
    async def startup() -> dict[str, object]:
        raise RuntimeError("startup failed")

    async def scroll() -> dict[str, object]:
        return {"scenario_type": "scroll", "metrics": ["frame_time"]}

    result = await execute_smartperfetto_stage(
        scenarios=("startup", "scroll"),
        execute={"startup": startup, "scroll": scroll},
        guard=_guard,
        progress=lambda _summary: None,
    )

    assert result.state == "degraded"
    assert result.evidence == {
        "completed": {"scroll": {"scenario_type": "scroll", "metrics": ["frame_time"]}},
        "failures": {"startup": "smartperfetto_scenario_failed"},
    }


@pytest.mark.asyncio
async def test_source_mismatch_degrades_without_losing_trace_evidence() -> None:
    trace = {"findings": ["startup-block"]}

    async def source(_trace):
        return None, "source_package_mismatch"

    result = await execute_source_stage(
        trace_evidence=trace,
        execute=source,
        guard=_guard,
        progress=lambda _summary: None,
    )

    assert result == StageResult(
        state="degraded",
        evidence={"trace": trace, "source": None},
        failure_code="source_package_mismatch",
        progress_summary="源码与目标应用不匹配，继续生成 Trace 报告",
    )


@pytest.mark.asyncio
async def test_ai_invalid_keeps_validated_core_evidence() -> None:
    core = {"schema_version": "2.0", "findings": ["startup-block"]}

    async def invalid_ai(_core):
        raise ValueError("invalid candidate")

    result = await execute_ai_stage(
        validated_projection=core,
        execute=invalid_ai,
        guard=_guard,
        progress=lambda _summary: None,
    )

    assert result.state == "failed"
    assert result.evidence == core
    assert result.failure_code == "ai_output_invalid"


@pytest.mark.asyncio
async def test_guard_runs_before_and_after_each_io_boundary() -> None:
    checks = 0
    operation_called = False

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise StageExecutionRejected("stage execution rejected")

    async def publish(_projection):
        nonlocal operation_called
        operation_called = True
        return {"report_version": 1}

    with pytest.raises(StageExecutionRejected, match="stage execution rejected"):
        await execute_report_stage(
            validated_projection={"schema_version": "2.0"},
            execute=publish,
            guard=guard,
            progress=lambda _summary: None,
        )

    assert operation_called is True
    assert checks == 2


@pytest.mark.asyncio
async def test_report_publish_uses_projection_once_without_provider_callback() -> None:
    projection = {"schema_version": "2.0", "projection_sha256": "safe"}
    calls: list[dict[str, object]] = []

    async def publish(value):
        calls.append(value)
        return {"report_version": 1}

    result = await execute_report_stage(
        validated_projection=projection,
        execute=publish,
        guard=_guard,
        progress=lambda _summary: None,
    )

    assert calls == [projection]
    assert result.state == "completed"
    assert result.evidence == {"report_version": 1}


@pytest.mark.asyncio
async def test_cancellation_is_never_converted_to_stage_failure() -> None:
    async def canceled(_projection):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await execute_ai_stage(
            validated_projection={},
            execute=canceled,
            guard=_guard,
            progress=lambda _summary: None,
        )
