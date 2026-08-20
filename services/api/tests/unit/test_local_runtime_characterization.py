from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_lifecycle import (
    AnalysisLifecycleError,
    LifecycleSnapshot,
)
from perfpilot_api.local_app import (
    _InputDescriptor,
    _LocalAnalysis,
    _LocalInput,
    _LocalRuntime,
    _LocalUpload,
    _validate_persisted_state_shape,
)
from perfpilot_api.reports.contracts import canonical_json_bytes


TEAM_ID = UUID("21000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("31000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("71000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
CHECKSUM = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _runtime(tmp_path: Path) -> _LocalRuntime:
    return _LocalRuntime(
        gateway=object(),
        synthesizer=None,
        device_probe=object(),
        device_capture_gateway=None,
        memory_analysis_gateway=None,
        data_root=tmp_path,
        public_origin="http://localhost:8000",
        poll_interval_seconds=0,
        source_tasks=object(),
        source_artifacts=object(),
        source_wait_seconds=None,
        device_directory=object(),
        agent_tasks=object(),
        agent_artifacts=object(),
        apk_inspector=None,
    )


def _trace(*, schema: str = "1.0", match: str = "none") -> _LocalAnalysis:
    analysis = _LocalAnalysis(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        profile="startup",
        question="为什么启动慢？",
        inputs={
            "trace": _LocalInput(
                _InputDescriptor(
                    kind="trace",
                    mime="application/octet-stream",
                    size=4096,
                    sha256_b64=CHECKSUM,
                ),
                upload_id="41000000-0000-4000-8000-000000000001",
                artifact_id="51000000-0000-4000-8000-000000000001",
                finalized=True,
            )
        },
        trace_test_type="cold_start",
        target_package_name="com.rivotek.mediacenter",
        created_at=NOW,
        state="analyzing",
        response_schema_version=schema,
    )
    if schema == "1.3":
        analysis.source_code_analysis = {
            "requested": True,
            "provider_kind": "agent_workspace",
            "agent_id": str(AGENT_ID),
            "workspace_id": "92000000-0000-4000-8000-000000000001",
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": None,
            "context_state": "available" if match == "strong" else "mismatch",
            "match_summary": match,
            "verification_state": "verified" if match == "strong" else "mismatch",
            "failure_code": None if match == "strong" else "source_package_mismatch",
        }
    return analysis


def _script_device() -> _LocalAnalysis:
    return _LocalAnalysis(
        team_id=TEAM_ID,
        analysis_id=UUID(int=ANALYSIS_ID.int + 1),
        profile="startup",
        question=None,
        inputs={},
        analysis_mode="device",
        device_id=DEVICE_ID,
        device_agent_id=AGENT_ID,
        capture_configuration={
            "test_type": "cold_start",
            "launch_mode": "automatic",
            "duration_seconds": 15,
            "package_name": "com.rivotek.mediacenter",
            "launch_activity": "com.rivotek.mediacenter/mediacenteractivity",
        },
        created_at=NOW,
        state="queued",
        response_schema_version="1.3",
    )


def _remote_device(runtime: _LocalRuntime, tmp_path: Path) -> _LocalAnalysis:
    analysis = _LocalAnalysis(
        team_id=TEAM_ID,
        analysis_id=UUID(int=ANALYSIS_ID.int + 2),
        profile="startup",
        question=None,
        inputs={
            "apk": _LocalInput(
                _InputDescriptor(
                    kind="apk",
                    mime="application/vnd.android.package-archive",
                    size=1024,
                    sha256_b64=CHECKSUM,
                ),
                upload_id="41000000-0000-4000-8000-000000000002",
                artifact_id="51000000-0000-4000-8000-000000000002",
                finalized=True,
            )
        },
        analysis_mode="device",
        device_id=DEVICE_ID,
        device_agent_id=AGENT_ID,
        created_at=NOW,
        state="queued",
        response_schema_version="1.1",
    )
    upload = _LocalUpload(
        upload_id=analysis.inputs["apk"].upload_id or "",
        team_id=TEAM_ID,
        analysis_id=analysis.analysis_id,
        kind="apk",
        mime="application/vnd.android.package-archive",
        size=1024,
        sha256_b64=CHECKSUM,
        token="characterization-token",
        path=tmp_path / "input.apk",
        expires_at=NOW + timedelta(hours=1),
    )
    runtime.analyses[(TEAM_ID, analysis.analysis_id)] = analysis
    runtime.uploads[(TEAM_ID, analysis.analysis_id, upload.upload_id)] = upload
    return analysis


def test_public_and_persisted_documents_characterize_all_local_modes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    analyses = (
        _trace(),
        _script_device(),
        _remote_device(runtime, tmp_path),
        _trace(schema="1.3", match="strong"),
        _trace(schema="1.3", match="none"),
    )

    for analysis in analyses:
        public = runtime.response(analysis)
        persisted = runtime._state_document(analysis)
        _validate_persisted_state_shape(persisted)
        assert canonical_json_bytes(public).decode("utf-8").startswith("{")
        assert canonical_json_bytes(persisted).decode("utf-8").startswith("{")
        assert public["analysis_id"] == str(analysis.analysis_id)
        assert public["state"] == analysis.state
        assert persisted["generation"] == analysis.generation
        serialized = canonical_json_bytes(public).decode("utf-8")
        assert not re.search(r"(?:/Users/|/tmp/|characterization-token)", serialized)


def test_report_visibility_and_terminal_state_close_together(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    analysis = _trace(schema="1.3", match="strong")
    analysis.report = {"schema_version": "1.2"}
    analysis.state = "completed"
    analysis.completed_at = NOW
    analysis.runtime_status = {
        **dict(analysis.runtime_status or {}),
        "current_stage": "report",
        "stage_state": "completed",
        "available_actions": [],
    }
    public = runtime.response(analysis)
    persisted = runtime._state_document(analysis)

    assert public["state"] == "completed"
    assert public["report_available"] is True
    assert persisted["state"] == "completed"
    assert persisted["report_available"] is True


def test_cancel_and_generation_rejections_keep_stable_errors(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    analysis = _trace()
    analysis.cancel_requested_at = NOW

    with pytest.raises(AnalysisLifecycleError, match="analysis transition rejected"):
        runtime.lifecycle.transition(
            LifecycleSnapshot(
                analysis_id=analysis.analysis_id,
                state="analyzing",
                generation=analysis.generation,
                cancel_requested_at=analysis.cancel_requested_at,
                report_available=False,
            ),
            target="completed",
            now=NOW,
            result_generation=analysis.generation,
            publish_report=True,
        )
    with pytest.raises(AnalysisLifecycleError, match="analysis generation rejected"):
        runtime.lifecycle.transition(
            LifecycleSnapshot(
                analysis_id=analysis.analysis_id,
                state="analyzing",
                generation=analysis.generation,
                cancel_requested_at=None,
                report_available=False,
            ),
            target="completed",
            now=NOW,
            result_generation=99,
            publish_report=True,
        )


def test_module_boundaries_do_not_emit_private_runtime_values(tmp_path: Path) -> None:
    document = _runtime(tmp_path).response(_trace(schema="1.3", match="strong"))
    encoded = canonical_json_bytes(document).decode("utf-8")

    assert "token" not in encoded.casefold()
    assert "source content" not in encoded.casefold()
    assert "/Users/" not in encoded


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_dependency_direction_keeps_runtime_modules_http_independent() -> None:
    root = Path(__file__).parents[4]
    package = root / "services/api/src/perfpilot_api"
    independent = (
        "local_analysis_projection.py",
        "local_analysis_recovery.py",
        "local_analysis_lifecycle.py",
        "local_task_supervisor.py",
        "local_remote_capture.py",
        "local_stage_execution.py",
    )

    for name in independent:
        imports = _module_imports(package / name)
        assert "perfpilot_api.local_app" not in imports
        assert not any(module == "fastapi" or module.startswith("fastapi.") for module in imports)

    app_imports = _module_imports(package / "local_app.py")
    for name in independent:
        module = f"perfpilot_api.{Path(name).stem}"
        assert module in app_imports

    frontend_parser = (root / "app/lib/perfpilot-analysis-api.ts").read_text(
        encoding="utf-8"
    )
    assert 'from "./perfpilot-api"' not in frontend_parser


def test_module_boundaries_remove_migrated_private_implementations() -> None:
    root = Path(__file__).parents[4]
    local_app = (root / "services/api/src/perfpilot_api/local_app.py").read_text(
        encoding="utf-8"
    )
    analysis_progress = (root / "app/components/analysis-progress.tsx").read_text(
        encoding="utf-8"
    )

    migrated = (
        "_restore_remote_capture",
        "_retry_remote_publication",
        "_remote_device_definition",
        "_script_device_definition",
        "_synthesis_from_core",
        "_normalize_local_smartperfetto_result",
        "_prepare_local_report",
        "_compose_local_report",
        "_local_source_code_document",
        "_prepared_from_persisted_documents",
    )
    for name in migrated:
        assert f"def {name}(" not in local_app
    assert "function analysisStillChanging(" not in analysis_progress


def test_module_boundaries_keep_extracted_modules_focused() -> None:
    root = Path(__file__).parents[4]
    package = root / "services/api/src/perfpilot_api"
    local_app_lines = (package / "local_app.py").read_text(encoding="utf-8").count("\n") + 1
    assert local_app_lines <= 6_032

    for name in (
        "local_analysis_projection.py",
        "local_analysis_recovery.py",
        "local_remote_capture.py",
        "local_stage_execution.py",
    ):
        lines = (package / name).read_text(encoding="utf-8").count("\n") + 1
        assert lines < 1_200, name
