"""Canonical, redacted validation for report documents."""

from __future__ import annotations

import json
import math
import numbers
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator, FormatChecker, validators

from perfpilot_api.reports.semantics import validate_source_aware_semantics


ContractName = Literal[
    "analysis-report",
    "normalized-trace-report",
    "analysis-projection",
    "synthesis-output",
]

_CONTRACT_SCHEMAS: dict[ContractName, str] = {
    "analysis-report": "reports/analysis-report.schema.json",
    "normalized-trace-report": "reports/normalized-trace-report.schema.json",
    "analysis-projection": "ai/analysis-projection.schema.json",
    "synthesis-output": "ai/synthesis-output.schema.json",
}
_CONTRACT_ROOT = Path(__file__).resolve().parents[5] / "contracts" / "v1"


def _is_finite_number(_checker: object, instance: object) -> bool:
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    if isinstance(instance, float):
        return math.isfinite(instance)
    if not isinstance(instance, numbers.Number):
        return False
    try:
        return math.isfinite(instance)
    except (OverflowError, TypeError, ValueError):
        return False


_FINITE_NUMBER_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine(
    "number",
    _is_finite_number,
)
_FiniteDraft202012Validator = validators.extend(
    Draft202012Validator,
    type_checker=_FINITE_NUMBER_CHECKER,
)


class ReportContractError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("report contract is invalid")


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ReportContractError from None


@lru_cache
def _validator(name: ContractName) -> Draft202012Validator:
    try:
        relative_path = _CONTRACT_SCHEMAS[name]
        schema = json.loads((_CONTRACT_ROOT / relative_path).read_text(encoding="utf-8"))
        return _FiniteDraft202012Validator(schema, format_checker=FormatChecker())
    except (KeyError, OSError, UnicodeError, ValueError):
        raise ReportContractError from None


def validate_contract(name: ContractName, value: object) -> dict[str, object]:
    try:
        copied = json.loads(canonical_json_bytes(value))
        _validator(name).validate(copied)
        if not isinstance(copied, dict):
            raise ReportContractError
        validate_source_aware_semantics(name, copied)
    except Exception:
        raise ReportContractError from None
    return copied
