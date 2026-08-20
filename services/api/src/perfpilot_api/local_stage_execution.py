from __future__ import annotations

import inspect
import base64
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar
from uuid import UUID, uuid4, uuid5

from perfpilot_api.engines.canonical_results import (
    EngineResultWrite,
    canonicalize_engine_result,
    result_artifact_id,
)
from perfpilot_api.engines.contracts import EngineResult
from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract
from perfpilot_api.reports.memory_join import (
    AndroidMemoryNormalizationError,
    join_android_memory_result,
    join_unavailable_android_memory,
)
from perfpilot_api.reports.normalizer import (
    NormalizedTraceReport,
    SmartPerfettoNormalizationError,
    normalize_smartperfetto_result,
)
from perfpilot_api.reports.projection import (
    AIProjection,
    ProjectionPrivacyError,
    ProjectionQuestionError,
    ProjectionSizeError,
    build_ai_projection,
)
from perfpilot_api.reports.smartperfetto_live_normalizer import (
    normalize_live_smartperfetto_result,
)
from perfpilot_api.services.canonical_result_reader import LoadedCanonicalResult


StageState = Literal["completed", "degraded", "failed", "canceled"]
T = TypeVar("T")

_SMARTPERFETTO_COMMIT = "1508f99788bfcf18cc861e4bf4f8b472e84240c3"
_ENGINE_IMAGE_DIGEST = "sha256:" + hashlib.sha256(
    _SMARTPERFETTO_COMMIT.encode()
).hexdigest()
_LOCAL_RECOVERY_NAMESPACE = UUID("e2ac7e9c-50e3-5d78-bd3f-53a56e2b2978")


class _LocalAnalysis(Protocol):
    pass


@dataclass(frozen=True, slots=True)
class _PreparedLocalReport:
    core_document: dict[str, object]
    projection: AIProjection
    projection_failure_code: str | None
    canonical_artifact_id: UUID
    canonical_sha256_b64: str
    normalizer_version: str
    source_report: dict[str, object]
    original_report_html_bytes: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _NormalizedLocalResult:
    report: NormalizedTraceReport
    artifact_id: UUID
    canonical_sha256_b64: str
    source_report: dict[str, object]
    original_report_html_bytes: bytes | None = field(default=None, repr=False)


def _validated_checksum(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise ValueError("invalid checksum") from None
    if len(decoded) != hashlib.sha256().digest_size:
        raise ValueError("invalid checksum")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("invalid checksum")
    return value


def _sha256_b64(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


def _blocked_ai_projection(
    *,
    analysis_id: UUID,
    analysis_profile: str,
    canonical_artifact_id: UUID,
) -> AIProjection:
    document = validate_contract(
        "analysis-projection",
        {
            "schema_version": "2.0",
            "analysis_id": str(analysis_id),
            "analysis_profile": analysis_profile,
            "question": None,
            "source": {
                "engine_id": "smartperfetto",
                "adapter_version": "privacy-blocked",
                "source_contract": "workspace-agent-v1",
                "canonical_artifact_id": str(canonical_artifact_id),
            },
            "scenarios": [],
            "limitations": [],
            "source_context": None,
        },
    )
    payload = canonical_json_bytes(document)
    return AIProjection(
        canonical_bytes=payload,
        sha256_b64=_sha256_b64(payload),
    )


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


def _synthesis_from_core(
    core: Mapping[str, object],
    upstream_report: Mapping[str, object],
) -> dict[str, object]:
    scenario_values = core.get("scenario_reports")
    scenarios = scenario_values if isinstance(scenario_values, list) else []
    findings: list[tuple[Mapping[str, object], str]] = []
    metric_groups: list[tuple[str, list[Mapping[str, object]], str | None]] = []
    for raw_scenario in scenarios:
        if not isinstance(raw_scenario, Mapping):
            continue
        scenario_type = str(raw_scenario.get("scenario_type", "startup"))
        scenario_findings = raw_scenario.get("findings")
        first_retest: str | None = None
        if isinstance(scenario_findings, list):
            for raw_finding in scenario_findings:
                if isinstance(raw_finding, Mapping):
                    findings.append((raw_finding, scenario_type))
                    retest = raw_finding.get("retest")
                    if first_retest is None and isinstance(retest, str) and retest.strip():
                        first_retest = retest.strip()[:2000]
        raw_metrics = raw_scenario.get("metrics")
        metrics = [item for item in raw_metrics or [] if isinstance(item, Mapping)]
        metric_groups.append((scenario_type, metrics, first_retest))

    top_findings: list[dict[str, object]] = []
    recommendations: list[dict[str, object]] = []
    for finding, _scenario_type in findings:
        finding_id = finding.get("finding_id")
        evidence = finding.get("evidence_ids")
        if not isinstance(finding_id, str) or not isinstance(evidence, list) or not evidence:
            continue
        evidence_ids = [item for item in evidence if isinstance(item, str)][:20]
        if not evidence_ids:
            continue
        summary = str(finding.get("summary") or finding.get("title") or "性能问题")[:2000]
        if len(top_findings) < 5:
            top_findings.append(
                {
                    "finding_id": finding_id,
                    "evidence_ids": evidence_ids,
                    "user_impact": summary,
                }
            )
        recommendation = finding.get("recommendation")
        action = (
            recommendation.strip()
            if isinstance(recommendation, str) and recommendation.strip()
            else "根据关联证据修复该问题，并在相同场景下复测。"
        )
        severity = str(finding.get("severity", "informational"))
        priority = {"critical": "p0", "warning": "p1", "healthy": "p3"}.get(
            severity, "p2"
        )
        if len(recommendations) < 10:
            recommendations.append(
                {
                    "priority": priority,
                    "title": str(finding.get("title") or "优化性能问题")[:2000],
                    "action": action[:2000],
                    "expected_effect": f"降低“{str(finding.get('title') or '该问题')}”对体验的影响。"[
                        :2000
                    ],
                    "finding_ids": [finding_id],
                    "evidence_ids": evidence_ids,
                }
            )

    retest_plan: list[dict[str, object]] = []
    for scenario_type, metrics, first_retest in metric_groups:
        available = [
            metric
            for metric in metrics
            if metric.get("status") == "available" and isinstance(metric.get("metric_id"), str)
        ]
        if not available or len(retest_plan) >= 5:
            continue
        retest_plan.append(
            {
                "mode": "verify_metric",
                "scenario_type": scenario_type,
                "metric_ids": [str(metric["metric_id"]) for metric in available[:20]],
                "limitation_ids": [],
                "steps": first_retest or "使用相同设备与场景重新采集 Trace，并比较关键指标。",
                "success_condition": (
                    "meet_existing_threshold"
                    if any(metric.get("threshold") is not None for metric in available)
                    else "improve_from_baseline"
                ),
                "failure_condition": "threshold_missed",
            }
        )

    raw_limitations = core.get("limitations")
    limitations = [
        {"limitation_id": item["limitation_id"], "summary": item["summary"]}
        for item in (raw_limitations if isinstance(raw_limitations, list) else [])
        if isinstance(item, Mapping)
        and isinstance(item.get("limitation_id"), str)
        and isinstance(item.get("summary"), str)
    ][:20]
    summary = upstream_report.get("summary")
    conclusion = summary.get("conclusion") if isinstance(summary, Mapping) else None
    executive_summary = (
        conclusion.strip()[:2000]
        if isinstance(conclusion, str) and conclusion.strip()
        else f"SmartPerfetto 已完成分析，确认 {len(top_findings)} 个有证据支持的问题。"
    )
    return validate_contract(
        "synthesis-output",
        {
            "schema_version": "1.0",
            "executive_summary": executive_summary,
            "top_findings": top_findings,
            "recommendations": recommendations,
            "retest_plan": retest_plan,
            "limitations": limitations,
        },
    )


def _normalize_local_smartperfetto_result(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    profile: str,
    input_sha256_b64: str | None = None,
) -> _NormalizedLocalResult:
    execution_id = uuid4()
    artifact_id = result_artifact_id(execution_id)
    if input_sha256_b64 is None:
        source_input = analysis.inputs[
            "trace" if analysis.analysis_mode == "trace_upload" else "apk"
        ].descriptor
        input_sha256_b64 = source_input.sha256_b64
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=analysis.team_id,
            analysis_id=analysis.analysis_id,
            execution_id=execution_id,
            expected_execution_version=1,
            tenant_resource_version=1,
            artifact_id=artifact_id,
            engine_id="smartperfetto",
            adapter_version="1.0.0",
            engine_commit_sha=_SMARTPERFETTO_COMMIT,
            engine_image_digest=_ENGINE_IMAGE_DIGEST,
            attempt_number=1,
            input_manifest_hash=hashlib.sha256(
                input_sha256_b64.encode()
            ).hexdigest(),
            config_hash=hashlib.sha256(
                f"{profile}\0{analysis.question or ''}".encode()
            ).hexdigest(),
            result=result,
        )
    )
    loaded = LoadedCanonicalResult(
        team_id=analysis.team_id,
        analysis_id=analysis.analysis_id,
        execution_id=execution_id,
        artifact_id=artifact_id,
        tenant_resource_version=1,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )
    try:
        normalized = normalize_smartperfetto_result(
            loaded,
            analysis_mode=analysis.analysis_mode,
        )
    except SmartPerfettoNormalizationError:
        normalized = normalize_live_smartperfetto_result(
            loaded,
            analysis_mode=analysis.analysis_mode,
        )
    if analysis.analysis_mode == "trace_upload" and analysis.target_package_name:
        document = normalized.document
        mismatched = False
        for scenario in document["scenario_reports"]:
            target = scenario["trace_health"]["target_resolution"]
            observed = target["package_name"]
            if observed == analysis.target_package_name:
                continue
            mismatched = True
            scenario["core_state"] = "partial"
            scenario["metrics"] = []
            scenario["findings"] = []
            scenario["evidence"] = []
        if mismatched:
            limitation_id = uuid5(
                _LOCAL_RECOVERY_NAMESPACE,
                f"{analysis.analysis_id}:target-package-mismatch",
            )
            document["core_state"] = "partial"
            document["limitations"].append(
                {
                    "limitation_id": str(limitation_id),
                    "code": "smartperfetto.target_package_mismatch",
                    "summary": (
                        "Trace 中识别到的目标应用与请求包名不一致；"
                        f"仅接受 {analysis.target_package_name} 的证据，本次未输出其他应用的问题结论。"
                    ),
                    "evidence_ids": [],
                }
            )
            document["limitations"].sort(key=lambda item: item["limitation_id"])
            validated = validate_contract("normalized-trace-report", document)
            canonical_bytes = canonical_json_bytes(validated)
            normalized = NormalizedTraceReport(
                canonical_bytes=canonical_bytes,
                sha256_b64=base64.b64encode(
                    hashlib.sha256(canonical_bytes).digest()
                ).decode("ascii"),
            )
    report_payload = result.payload.get("report")
    if not isinstance(report_payload, Mapping):
        raise ValueError("SmartPerfetto report is invalid")
    return _NormalizedLocalResult(
        report=normalized,
        artifact_id=artifact_id,
        canonical_sha256_b64=canonical.checksum_sha256_b64,
        source_report=dict(report_payload),
        original_report_html_bytes=result.original_report_html_bytes,
    )


def _remote_capture_question(
    analysis: _LocalAnalysis,
    *,
    scenario_type: Literal["startup", "scroll"],
) -> str | None:
    configuration = analysis.capture_configuration
    package_name = (
        configuration.get("package_name")
        if isinstance(configuration, Mapping)
        else None
    )
    if not isinstance(package_name, str) or not package_name:
        return analysis.question
    scenario_name = "启动" if scenario_type == "startup" else "滑动"
    return (
        f"请仅分析目标应用 {package_name} 的{scenario_name}性能；"
        "若 Trace 不包含该应用数据，请明确说明，不要替换成其他应用。"
    )


def _trace_upload_question(analysis: _LocalAnalysis) -> str | None:
    if analysis.target_package_name is None or analysis.trace_test_type is None:
        return analysis.question
    scenario = {
        "cold_start": "cold start",
        "hot_start": "hot start",
        "scroll": "scrolling",
        "other": analysis.custom_test_name or "custom workflow",
    }[analysis.trace_test_type]
    parts = [
        f"Only analyze Android package {analysis.target_package_name}.",
        f"The captured scenario is {scenario}.",
        "Ignore unrelated processes and state when target evidence is insufficient.",
    ]
    if analysis.trace_test_type == "other" and analysis.custom_test_description:
        parts.append(f"Custom workflow purpose: {analysis.custom_test_description}")
    if analysis.question:
        parts.append(f"Additional analysis context: {analysis.question}")
    return "\n\n".join(parts)


def _missing_scroll_scenario(
    team_id: UUID, analysis_id: UUID
) -> tuple[dict[str, object], dict[str, object]]:
    scenario_id = str(
        uuid5(
            _LOCAL_RECOVERY_NAMESPACE,
            f"{team_id}:{analysis_id}:scroll-unavailable",
        )
    )
    limitation_id = str(
        uuid5(
            _LOCAL_RECOVERY_NAMESPACE,
            f"{team_id}:{analysis_id}:scroll-result-unavailable",
        )
    )
    return (
        {
            "scenario_id": scenario_id,
            "scenario_type": "scroll",
            "core_state": "partial",
            "metrics": [],
            "findings": [],
            "evidence": [],
            "trace_health": {
                "parse_status": "failed",
                "trace_start_ns": None,
                "trace_end_ns": None,
                "target_resolution": {
                    "package_name": None,
                    "process_name": None,
                    "upid": None,
                    "pid": None,
                    "main_thread_id": None,
                },
                "measurement_window": {
                    "start_ns": None,
                    "end_ns": None,
                    "coverage": "missing",
                },
                "data_loss": {
                    "buffer_overruns": 0,
                    "ftrace_events_lost": 0,
                    "traced_buf_patches_failed": 0,
                    "incomplete_slices": 0,
                    "boundary_truncations": 0,
                },
                "frame_timeline_coverage": "unavailable",
                "target_display_coverage": "unavailable",
                "refresh_mode_coverage": "unavailable",
            },
            "trace_capabilities": [
                {
                    "name": "smartperfetto_scroll_result",
                    "required": True,
                    "status": "unavailable",
                    "reason": "SmartPerfetto did not produce a usable scroll result.",
                }
            ],
        },
        {
            "limitation_id": limitation_id,
            "code": "smartperfetto.scroll_result_unavailable",
            "summary": "滑动 Trace 已采集，但 SmartPerfetto 未返回可用的滑动分析结果。",
            "evidence_ids": [],
        },
    )


def _missing_remote_capture_scenario(
    team_id: UUID,
    analysis_id: UUID,
    scenario_type: Literal["startup", "scroll"],
    *,
    reason: Literal["capture_failed", "smartperfetto_failed"] = "capture_failed",
) -> tuple[dict[str, object], dict[str, object]]:
    scenario_id = str(
        uuid5(
            _LOCAL_RECOVERY_NAMESPACE,
            f"{team_id}:{analysis_id}:{scenario_type}-capture-unavailable",
        )
    )
    limitation_id = str(
        uuid5(
            _LOCAL_RECOVERY_NAMESPACE,
            f"{team_id}:{analysis_id}:{scenario_type}-capture-failed",
        )
    )
    label = "冷启动" if scenario_type == "startup" else "滑动"
    capability = (
        f"remote_{scenario_type}_capture"
        if reason == "capture_failed"
        else f"smartperfetto_{scenario_type}_result"
    )
    reason_text = (
        f"Remote {scenario_type} capture did not complete."
        if reason == "capture_failed"
        else f"SmartPerfetto could not analyze the {scenario_type} trace."
    )
    limitation_code = (
        f"remote_capture.{scenario_type}_unavailable"
        if reason == "capture_failed"
        else f"smartperfetto.{scenario_type}_result_unavailable"
    )
    limitation_summary = (
        f"{label}采集未完成，本报告仅保留已成功场景的分析证据。"
        if reason == "capture_failed"
        else f"{label} Trace 已采集，但 SmartPerfetto 未返回可用结果；本报告保留其他场景证据。"
    )
    return (
        {
            "scenario_id": scenario_id,
            "scenario_type": scenario_type,
            "core_state": "partial",
            "metrics": [],
            "findings": [],
            "evidence": [],
            "trace_health": {
                "parse_status": "failed",
                "trace_start_ns": None,
                "trace_end_ns": None,
                "target_resolution": {
                    "package_name": None,
                    "process_name": None,
                    "upid": None,
                    "pid": None,
                    "main_thread_id": None,
                },
                "measurement_window": {
                    "start_ns": None,
                    "end_ns": None,
                    "coverage": "missing",
                },
                "data_loss": {
                    "buffer_overruns": 0,
                    "ftrace_events_lost": 0,
                    "traced_buf_patches_failed": 0,
                    "incomplete_slices": 0,
                    "boundary_truncations": 0,
                },
                "frame_timeline_coverage": "unavailable",
                "target_display_coverage": "unavailable",
                "refresh_mode_coverage": "unavailable",
            },
            "trace_capabilities": [
                {
                    "name": capability,
                    "required": True,
                    "status": "unavailable",
                    "reason": reason_text,
                }
            ],
        },
        {
            "limitation_id": limitation_id,
            "code": limitation_code,
            "summary": limitation_summary,
            "evidence_ids": [],
        },
    )


def _project_remote_capture_scenarios(
    team_id: UUID,
    report: NormalizedTraceReport,
    completed_scenarios: frozenset[str],
    failures: Mapping[str, Literal["capture_failed", "smartperfetto_failed"]]
    | None = None,
    requested_scenarios: frozenset[str] = frozenset({"startup", "scroll"}),
) -> NormalizedTraceReport:
    document = validate_contract("normalized-trace-report", report.document)
    selected = {
        str(item["scenario_type"]): dict(item)
        for item in document["scenario_reports"]
        if isinstance(item, Mapping)
        and item.get("scenario_type") in completed_scenarios
        and item.get("scenario_type") in requested_scenarios
    }
    limitations = [
        dict(item) for item in document["limitations"] if isinstance(item, Mapping)
    ]
    for scenario_type in ("startup", "scroll"):
        if scenario_type not in requested_scenarios:
            continue
        if scenario_type not in completed_scenarios:
            scenario, limitation = _missing_remote_capture_scenario(
                team_id,
                UUID(str(document["analysis_id"])),
                scenario_type,
                reason=(failures or {}).get(scenario_type, "capture_failed"),
            )
            selected[scenario_type] = scenario
            limitations.append(limitation)
    scenarios = [selected[item] for item in ("startup", "scroll") if item in selected]
    projected = {
        **document,
        "core_state": (
            "partial"
            if completed_scenarios != requested_scenarios
            or any(item.get("core_state") == "partial" for item in scenarios)
            else "complete"
        ),
        "scenario_reports": scenarios,
        "limitations": limitations[:20],
    }
    validated = validate_contract("normalized-trace-report", projected)
    payload = canonical_json_bytes(validated)
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=_sha256_b64(payload),
    )


def _merge_local_smartperfetto_reports(
    team_id: UUID,
    primary: NormalizedTraceReport,
    secondary: NormalizedTraceReport,
) -> NormalizedTraceReport:
    primary_document = validate_contract("normalized-trace-report", primary.document)
    secondary_document = validate_contract("normalized-trace-report", secondary.document)
    if (
        primary_document["analysis_id"] != secondary_document["analysis_id"]
        or primary_document["analysis_mode"] != "device"
        or secondary_document["analysis_mode"] != "device"
    ):
        raise ValueError("SmartPerfetto device reports cannot be merged")
    selected: dict[str, dict[str, object]] = {}
    for raw in primary_document["scenario_reports"]:
        if isinstance(raw, Mapping):
            selected[str(raw["scenario_type"])] = dict(raw)
    for raw in secondary_document["scenario_reports"]:
        if isinstance(raw, Mapping) and (
            raw.get("scenario_type") == "scroll"
            or str(raw.get("scenario_type")) not in selected
        ):
            selected[str(raw["scenario_type"])] = dict(raw)
    limitations: dict[str, dict[str, object]] = {}
    for raw in [
        *primary_document["limitations"],
        *secondary_document["limitations"],
    ]:
        if isinstance(raw, Mapping):
            limitations[str(raw["limitation_id"])] = dict(raw)
    if "scroll" not in selected:
        scenario, limitation = _missing_scroll_scenario(
            team_id,
            UUID(str(primary_document["analysis_id"]))
        )
        selected["scroll"] = scenario
        limitations[str(limitation["limitation_id"])] = limitation
    order = {"startup": 0, "scroll": 1, "memory_cycle": 2}
    scenarios = sorted(selected.values(), key=lambda item: order[str(item["scenario_type"])])
    merged = {
        **primary_document,
        "core_state": (
            "partial"
            if any(item.get("core_state") == "partial" for item in scenarios)
            else "complete"
        ),
        "scenario_reports": scenarios,
        "limitations": sorted(
            limitations.values(),
            key=lambda item: str(item["limitation_id"]),
        )[:20],
    }
    validated = validate_contract("normalized-trace-report", merged)
    payload = canonical_json_bytes(validated)
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=_sha256_b64(payload),
    )


def _canonical_local_memory_result(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    engine_commit_sha: str,
) -> LoadedCanonicalResult:
    execution_id = uuid4()
    artifact_id = result_artifact_id(execution_id)
    apk = analysis.inputs["apk"].descriptor
    canonical = canonicalize_engine_result(
        EngineResultWrite(
            team_id=analysis.team_id,
            analysis_id=analysis.analysis_id,
            execution_id=execution_id,
            expected_execution_version=1,
            tenant_resource_version=1,
            artifact_id=artifact_id,
            engine_id="android_memory",
            adapter_version="1.0.0",
            engine_commit_sha=engine_commit_sha,
            engine_image_digest=(
                "sha256:" + hashlib.sha256(engine_commit_sha.encode()).hexdigest()
            ),
            attempt_number=1,
            input_manifest_hash=hashlib.sha256(apk.sha256_b64.encode()).hexdigest(),
            config_hash=hashlib.sha256(b"auto\0local-device-memory").hexdigest(),
            result=result,
        )
    )
    return LoadedCanonicalResult(
        team_id=analysis.team_id,
        analysis_id=analysis.analysis_id,
        execution_id=execution_id,
        artifact_id=artifact_id,
        tenant_resource_version=1,
        sha256_b64=canonical.checksum_sha256_b64,
        document=canonical.document,
        canonical_bytes=canonical.canonical_bytes,
    )


def _prepare_local_report(
    analysis: _LocalAnalysis,
    result: EngineResult,
    *,
    primary_profile: Literal["startup", "scroll"] | None = None,
    primary_normalized: _NormalizedLocalResult | None = None,
    scroll_result: EngineResult | None = None,
    scroll_normalized: _NormalizedLocalResult | None = None,
    memory_result: EngineResult | None = None,
    memory_engine_commit_sha: str | None = None,
    source_context: Mapping[str, object] | None = None,
    include_memory: bool = True,
    remote_completed_scenarios: frozenset[str] | None = None,
    remote_scenario_failures: Mapping[
        str, Literal["capture_failed", "smartperfetto_failed"]
    ]
    | None = None,
) -> _PreparedLocalReport:
    primary = primary_normalized or _normalize_local_smartperfetto_result(
        analysis,
        result,
        profile=primary_profile or analysis.profile,
    )
    scroll: _NormalizedLocalResult | None = scroll_normalized
    normalized = primary.report
    if analysis.analysis_mode == "device":
        if scroll_result is not None or scroll is not None:
            if scroll is None:
                assert scroll_result is not None
                scroll = _normalize_local_smartperfetto_result(
                    analysis,
                    scroll_result,
                    profile="scroll",
                )
            normalized = _merge_local_smartperfetto_reports(
                analysis.team_id, normalized, scroll.report
            )
        elif analysis.capture_configuration is None:
            normalized = _merge_local_smartperfetto_reports(
                analysis.team_id, normalized, normalized
            )
        if remote_completed_scenarios is not None:
            normalized = _project_remote_capture_scenarios(
                analysis.team_id,
                normalized,
                remote_completed_scenarios,
                remote_scenario_failures,
                (
                    frozenset({"scroll"})
                    if analysis.capture_configuration is not None
                    and analysis.capture_configuration.get("test_type") == "scroll"
                    else frozenset({"startup"})
                    if analysis.capture_configuration is not None
                    else frozenset({"startup", "scroll"})
                ),
            )
        elif not include_memory:
            pass
        elif memory_result is not None and memory_engine_commit_sha is not None:
            try:
                normalized = join_android_memory_result(
                    normalized,
                    _canonical_local_memory_result(
                        analysis,
                        memory_result,
                        engine_commit_sha=memory_engine_commit_sha,
                    ),
                )
            except AndroidMemoryNormalizationError:
                normalized = join_unavailable_android_memory(
                    normalized,
                    reason="result_invalid",
                )
        else:
            normalized = join_unavailable_android_memory(
                normalized,
                reason="result_unavailable",
            )
    normalized_provenance = normalized.document.get("provenance")
    if not isinstance(normalized_provenance, Mapping) or not isinstance(
        normalized_provenance.get("normalizer_version"), str
    ):
        raise ValueError("SmartPerfetto normalization provenance is invalid")
    normalizer_version = normalized_provenance["normalizer_version"]
    projection_failure_code: str | None = None
    try:
        projection = build_ai_projection(
            normalized,
            analysis_profile=analysis.profile,  # type: ignore[arg-type]
            question=analysis.question,
            source_context=source_context,
        )
    except ProjectionPrivacyError:
        projection_failure_code = "ai_projection_private_data"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    except ProjectionQuestionError:
        projection_failure_code = "ai_projection_invalid_question"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    except ProjectionSizeError:
        projection_failure_code = "ai_projection_too_large"
        projection = _blocked_ai_projection(
            analysis_id=analysis.analysis_id,
            analysis_profile=analysis.profile,
            canonical_artifact_id=primary.artifact_id,
        )
    return _PreparedLocalReport(
        core_document=normalized.document,
        projection=projection,
        projection_failure_code=projection_failure_code,
        canonical_artifact_id=primary.artifact_id,
        canonical_sha256_b64=primary.canonical_sha256_b64,
        normalizer_version=normalizer_version,
        source_report=primary.source_report,
        original_report_html_bytes=primary.original_report_html_bytes,
    )

PreparedLocalReport = _PreparedLocalReport
NormalizedLocalResult = _NormalizedLocalResult
blocked_ai_projection = _blocked_ai_projection
prepare_local_report = _prepare_local_report
normalize_local_smartperfetto_result = _normalize_local_smartperfetto_result
remote_capture_question = _remote_capture_question
sha256_b64 = _sha256_b64
trace_upload_question = _trace_upload_question
synthesis_from_core = _synthesis_from_core
validated_checksum = _validated_checksum
REPORT_WORKER_IMAGE_DIGEST = _ENGINE_IMAGE_DIGEST


__all__ = [
    "NormalizedLocalResult",
    "PreparedLocalReport",
    "REPORT_WORKER_IMAGE_DIGEST",
    "blocked_ai_projection",
    "normalize_local_smartperfetto_result",
    "prepare_local_report",
    "remote_capture_question",
    "sha256_b64",
    "synthesis_from_core",
    "trace_upload_question",
    "validated_checksum",
    "StageExecutionRejected",
    "StageResult",
    "execute_ai_stage",
    "execute_report_stage",
    "execute_smartperfetto_stage",
    "execute_source_stage",
]
