"""Compose the immutable AnalysisReport 1.3 Finding workbench view."""

from __future__ import annotations

import json
from typing import Literal, Mapping

from perfpilot_api.reports.contracts import canonical_json_bytes, validate_contract


class FindingReportError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("finding report source is invalid")


def _copy(value: object) -> object:
    return json.loads(canonical_json_bytes(value))


def _scenario_complete(quality: Mapping[str, object]) -> bool:
    capabilities = quality.get("capabilities")
    return bool(
        quality.get("parse_status") == "parsed"
        and quality.get("measurement_window_coverage") == "complete"
        and quality.get("data_loss_present") is False
        and isinstance(capabilities, list)
        and all(
            not isinstance(item, Mapping)
            or item.get("required") is not True
            or item.get("status") == "available"
            for item in capabilities
        )
    )


def compose_finding_report(
    *,
    base_report: Mapping[str, object],
    projection: Mapping[str, object],
    synthesis: Mapping[str, object],
    report_version: int,
    ai_mode: Literal["available", "deterministic_fallback"] = "available",
) -> dict[str, object]:
    """Return a closed 1.3 document without mutating any input."""

    try:
        if type(report_version) is not int or report_version < 1:
            raise FindingReportError
        validated_projection = validate_contract("analysis-projection", projection)
        validated_synthesis = validate_contract("synthesis-output", synthesis)
        if (
            validated_projection.get("schema_version") != "2.1"
            or validated_synthesis.get("schema_version") != "2.1"
            or ai_mode not in {"available", "deterministic_fallback"}
        ):
            raise FindingReportError
        document = _copy(base_report)
        if not isinstance(document, dict):
            raise FindingReportError
        synthesis_section = document.get("synthesis")
        scenario_reports = document.get("scenario_reports")
        if not isinstance(synthesis_section, dict) or not isinstance(scenario_reports, list):
            raise FindingReportError
        quality = _copy(validated_projection["quality"])
        capabilities = _copy(validated_projection["capabilities"])
        workbench = _copy(validated_projection["workbench"])
        if not all(isinstance(value, dict) for value in (quality, capabilities, workbench)):
            raise FindingReportError
        quality["synthesis_state"] = "completed"
        capabilities["ai"] = ai_mode
        quality_by_scenario = {
            item["scenario_type"]: item
            for item in quality["scenarios"]
            if isinstance(item, dict) and isinstance(item.get("scenario_type"), str)
        }
        all_complete = quality.get("trace_core_state") == "complete"
        reason_codes = list(quality.get("reason_codes", []))
        for report in scenario_reports:
            if not isinstance(report, dict):
                raise FindingReportError
            scenario_quality = quality_by_scenario.get(report.get("scenario_type"))
            complete = isinstance(scenario_quality, Mapping) and _scenario_complete(
                scenario_quality
            )
            all_complete = all_complete and complete
            report["result_state"] = "completed" if complete else "partially_completed"
            report["failure"] = None
            bundle = report.get("bundle")
            if not isinstance(bundle, dict):
                raise FindingReportError
            bundle["bundle_state"] = "complete" if complete else "partial"
            bundle["valid_measurement"] = bool(
                isinstance(scenario_quality, Mapping)
                and scenario_quality.get("parse_status") == "parsed"
                and scenario_quality.get("measurement_window_coverage") != "missing"
            )
            bundle["validity_reasons"] = [] if complete else reason_codes
        synthesis_section["state"] = "completed"
        synthesis_section["output"] = _copy(validated_synthesis)
        synthesis_section["failure_code"] = None
        document.update(
            {
                "schema_version": "1.3",
                "state": "completed" if all_complete else "partially_completed",
                "report_version": report_version,
                "capabilities": capabilities,
                "quality": quality,
                "workbench": workbench,
            }
        )
        return validate_contract("analysis-report", document)
    except FindingReportError:
        raise
    except Exception:
        raise FindingReportError from None


__all__ = ["FindingReportError", "compose_finding_report"]
