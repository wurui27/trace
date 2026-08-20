from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar


StageState = Literal["completed", "degraded", "failed", "canceled"]
T = TypeVar("T")


class StageExecutionRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StageResult:
    state: StageState
    evidence: Mapping[str, object] | None
    failure_code: str | None
    progress_summary: str


Guard = Callable[[], None | Awaitable[None]]
Progress = Callable[[str], None | Awaitable[None]]


async def _call(callback: Callable[..., T | Awaitable[T]], *args: object) -> T:
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _guarded(
    *,
    guard: Guard,
    operation: Callable[[], Awaitable[T]],
) -> T:
    await _call(guard)
    result = await operation()
    await _call(guard)
    return result


async def execute_smartperfetto_stage(
    *,
    scenarios: tuple[str, ...],
    execute: Mapping[str, Callable[[], Awaitable[Mapping[str, object]]]],
    guard: Guard,
    progress: Progress,
) -> StageResult:
    if not scenarios or len(set(scenarios)) != len(scenarios):
        raise ValueError("SmartPerfetto scenarios rejected")
    completed: dict[str, Mapping[str, object]] = {}
    failures: dict[str, str] = {}
    for scenario in scenarios:
        operation = execute.get(scenario)
        if operation is None:
            raise ValueError("SmartPerfetto scenarios rejected")
        await _call(progress, f"SmartPerfetto 正在分析 {scenario}")
        try:
            completed[scenario] = await _guarded(
                guard=guard, operation=operation
            )
        except StageExecutionRejected:
            raise
        except Exception:
            await _call(guard)
            failures[scenario] = "smartperfetto_scenario_failed"
    state: StageState = (
        "completed" if not failures else "degraded" if completed else "failed"
    )
    return StageResult(
        state=state,
        evidence={"completed": completed, "failures": failures},
        failure_code=("smartperfetto_all_failed" if not completed else None),
        progress_summary=(
            "SmartPerfetto 分析完成"
            if not failures
            else "部分场景证据不足，已保留成功结果"
            if completed
            else "SmartPerfetto 未生成可用结果"
        ),
    )


async def execute_source_stage(
    *,
    trace_evidence: Mapping[str, object],
    execute: Callable[
        [Mapping[str, object]],
        Awaitable[tuple[Mapping[str, object] | None, str | None]],
    ],
    guard: Guard,
    progress: Progress,
) -> StageResult:
    await _call(progress, "正在读取并匹配源码")
    try:
        source, failure_code = await _guarded(
            guard=guard,
            operation=lambda: execute(trace_evidence),
        )
    except StageExecutionRejected:
        raise
    except Exception:
        return StageResult(
            state="degraded",
            evidence={"trace": trace_evidence, "source": None},
            failure_code="source_analysis_unavailable",
            progress_summary="源码读取失败，继续生成 Trace 报告",
        )
    if failure_code is not None or source is None:
        return StageResult(
            state="degraded",
            evidence={"trace": trace_evidence, "source": source},
            failure_code=failure_code or "source_analysis_unavailable",
            progress_summary=(
                "源码与目标应用不匹配，继续生成 Trace 报告"
                if failure_code == "source_package_mismatch"
                else "源码证据不足，继续生成 Trace 报告"
            ),
        )
    return StageResult(
        state="completed",
        evidence={"trace": trace_evidence, "source": source},
        failure_code=None,
        progress_summary="源码读取和匹配完成",
    )


async def execute_ai_stage(
    *,
    validated_projection: Mapping[str, object],
    execute: Callable[[Mapping[str, object]], Awaitable[Mapping[str, object]]],
    guard: Guard,
    progress: Progress,
) -> StageResult:
    await _call(progress, "正在生成中文分析结论")
    try:
        output = await _guarded(
            guard=guard,
            operation=lambda: execute(validated_projection),
        )
    except StageExecutionRejected:
        raise
    except ValueError:
        return StageResult(
            state="failed",
            evidence=validated_projection,
            failure_code="ai_output_invalid",
            progress_summary="中文总结输出无效，已保留核心 Trace 结论",
        )
    except Exception:
        return StageResult(
            state="failed",
            evidence=validated_projection,
            failure_code="ai_report_failed",
            progress_summary="中文总结生成失败，已保留核心 Trace 结论",
        )
    return StageResult(
        state="completed",
        evidence=output,
        failure_code=None,
        progress_summary="中文分析结论生成完成",
    )


async def execute_report_stage(
    *,
    validated_projection: Mapping[str, object],
    execute: Callable[
        [Mapping[str, object]], Awaitable[Mapping[str, object] | None]
    ],
    guard: Guard,
    progress: Progress,
    propagate_errors: bool = False,
) -> StageResult:
    await _call(progress, "正在发布最终报告")
    try:
        report = await _guarded(
            guard=guard,
            operation=lambda: execute(validated_projection),
        )
    except StageExecutionRejected:
        raise
    except Exception:
        if propagate_errors:
            raise
        return StageResult(
            state="failed",
            evidence=validated_projection,
            failure_code="report_publish_failed",
            progress_summary="报告发布失败，可从已保存证据恢复",
        )
    return StageResult(
        state="completed",
        evidence=report,
        failure_code=None,
        progress_summary="最终报告已发布",
    )


__all__ = [
    "StageExecutionRejected",
    "StageResult",
    "execute_ai_stage",
    "execute_report_stage",
    "execute_smartperfetto_stage",
    "execute_source_stage",
]
