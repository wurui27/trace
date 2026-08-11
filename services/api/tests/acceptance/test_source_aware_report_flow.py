from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_agent.security import SourceFindingHint
from perfpilot_agent.source_rules import select_source_context
from perfpilot_agent.source_snapshot import SourceSnapshotter
from perfpilot_api.ai.local_report import LocalReportSynthesizer
from perfpilot_api.ai.openai_compatible import SynthesisCandidate
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.reports.source_context import validate_source_context
from perfpilot_api.reports.writer import AnalysisReportWriteRequest, compose_analysis_report
from perfpilot_api.services.source_tasks import SourceTaskError
from perfpilot_api.workers.source_orchestrator import (
    InMemorySourceAnalysisStateRepository,
    SourceAnalysisAuthority,
    SourceOrchestrator,
)


ROOT = Path(__file__).resolve().parents[4]
TEAM_ID = UUID("11000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("82000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
SYNTHESIS_ID = UUID("22000000-0000-4000-8000-000000000001")
FINDING_ID = UUID("85000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("86000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
CHECKSUM = base64.b64encode(b"c" * 32).decode("ascii")


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "contracts/v1/examples" / name).read_text(encoding="utf-8")
    )


def _core() -> NormalizedTraceReport:
    payload = canonical_json_bytes(_load("normalized-trace-report.valid.json"))
    return NormalizedTraceReport(
        canonical_bytes=payload,
        sha256_b64=base64.b64encode(hashlib.sha256(payload).digest()).decode(),
    )


class OnePassProvider:
    provider_name = "acceptance-provider"
    model = "acceptance-model"
    prompt_version = "perfpilot-report-v3"
    prompt_sha256_b64 = CHECKSUM

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, projection) -> SynthesisCandidate:
        self.calls += 1
        candidate = _load("synthesis-output-v2.valid.json")
        candidate["source_fixes"] = []
        return SynthesisCandidate(canonical_json_bytes(candidate), 10, 20, 30)

    async def aclose(self) -> None:
        return None


def _source_not_requested() -> dict[str, object]:
    return {
        "requested": False,
        "provider_kind": None,
        "agent_id": None,
        "workspace_id": None,
        "snapshot_policy": None,
        "validation_profile_id": None,
        "snapshot": None,
        "context_state": "not_requested",
        "match_summary": "none",
        "source_refs": [],
        "exclusions": [],
        "fixes": [],
        "limitations": [],
    }


async def _report(
    *,
    source_context: dict[str, object] | None,
    source_document: dict[str, object],
) -> tuple[dict[str, object], OnePassProvider, dict[str, object]]:
    core = _core()
    projection = build_ai_projection(
        core,
        analysis_profile="auto",
        question=None,
        source_context=source_context,
    )
    provider = OnePassProvider()
    synthesis = await LocalReportSynthesizer(provider=provider).synthesize(projection)
    request = AnalysisReportWriteRequest(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        synthesis_execution_id=SYNTHESIS_ID,
        tenant_resource_version=1,
        generation=1,
        generated_at=NOW,
        core_document=core.document,
        synthesis_document=synthesis.output.document,
        synthesis_failure_code=None,
        canonical_artifact_id=UUID("85000000-0000-4000-8000-000000000001"),
        canonical_sha256_b64=CHECKSUM,
        projection_artifact_id=UUID("89000000-0000-4000-8000-000000000001"),
        projection_sha256_b64=projection.sha256_b64,
        synthesis_artifact_id=UUID("88000000-0000-4000-8000-000000000001"),
        synthesis_sha256_b64=synthesis.output.sha256_b64,
        normalizer_version="smartperfetto-normalizer-1",
        prompt_template_version=provider.prompt_version,
        prompt_template_sha256_b64=provider.prompt_sha256_b64,
        report_worker_image_digest="sha256:" + "1" * 64,
        provider_protocol="fake-one-pass",
        provider_name=provider.provider_name,
        model=provider.model,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency_ms=30,
        source_code_document=source_document,
    )
    report = compose_analysis_report(request, report_version=1).document
    return report, provider, projection.document


def _git(repo: Path, *arguments: str) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def _repository_digest(root: Path, *, git_only: bool = False) -> str:
    digest = hashlib.sha256()
    start = root / ".git" if git_only else root
    for path in sorted(item for item in start.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if not git_only and relative.startswith(".git/"):
            continue
        digest.update(relative.encode())
        digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(2, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_document(context: dict[str, object]) -> dict[str, object]:
    fragments = context["fragments"]
    assert isinstance(fragments, list)
    return {
        "requested": True,
        "provider_kind": "agent_workspace",
        "agent_id": str(AGENT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
        "snapshot": {
            "snapshot_id": context["snapshot_id"],
            "snapshot_hash": context["snapshot_hash"],
            "git_head": context["git_head"],
        },
        "context_state": "available",
        "match_summary": context["match_summary"],
        "source_refs": [
            {key: value for key, value in fragment.items() if key != "content"}
            | {"snapshot_hash": context["snapshot_hash"]}
            for fragment in fragments
        ],
        "exclusions": context["exclusions"],
        "fixes": [],
        "limitations": [],
    }


@pytest.mark.asyncio
async def test_trace_only_uses_one_provider_call_and_emits_concise_v12() -> None:
    report, provider, _projection = await _report(
        source_context=None,
        source_document=_source_not_requested(),
    )

    output = report["synthesis"]["output"]
    assert provider.calls == 1
    assert report["schema_version"] == "1.2"
    assert report["analysis_mode"] == "trace_upload"
    assert len(output["key_metric_ids"]) <= 3
    assert len(output["top_findings"]) <= 3
    assert len(output["recommendations"]) <= 3


@pytest.mark.asyncio
async def test_current_dirty_source_reaches_strong_v12_refs_without_mutating_git(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--initial-branch=main")
    source = repository / "app/src/main/java/demo/Startup.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package demo\nclass Startup { fun onCreate() = Unit }\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(
        repository,
        "-c",
        "user.name=PerfPilot Test",
        "-c",
        "user.email=perfpilot@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    source.write_text(
        "package demo\nclass Startup { fun onCreate() = Thread.sleep(42) } // DIRTY_SENTINEL\n",
        encoding="utf-8",
    )
    tree_before = _repository_digest(repository)
    git_before = _repository_digest(repository, git_only=True)
    snapshotter = SourceSnapshotter(cache_root=(tmp_path / "cache").resolve())
    snapshot = snapshotter.create(repository, WORKSPACE_ID, created_at=NOW)
    hint = SourceFindingHint(
        finding_id=FINDING_ID,
        evidence_ids=(EVIDENCE_ID,),
        rule_id="android.ui.blocking_wait",
        symbol_hints=("demo.Startup.onCreate",),
    )
    selected = select_source_context(snapshot, (hint,), max_files=12, max_bytes=98_304)
    raw = {
        "snapshot_id": str(snapshot.snapshot_id),
        "snapshot_hash": snapshot.snapshot_hash,
        "git_head": snapshot.git_head,
        "tracked_dirty_count": snapshot.tracked_dirty_count,
        "fragments": selected.fragment_documents(),
        "exclusions": [item.document() for item in selected.exclusions],
        "truncated": selected.truncated,
    }
    context = validate_source_context(
        raw,
        direct_identifiers=("demo.Startup.onCreate",),
        allowed_finding_ids=(str(FINDING_ID),),
        allowed_evidence_ids=(str(EVIDENCE_ID),),
    )

    report, provider, projection = await _report(
        source_context=context,
        source_document=_source_document(context),
    )
    snapshotter.mark_terminal(snapshot.snapshot_id)
    snapshotter.cleanup()

    assert provider.calls == 1
    assert context["match_summary"] == "strong"
    assert "DIRTY_SENTINEL" in projection["source_context"]["fragments"][0]["content"]
    assert report["schema_version"] == "1.2"
    assert report["source_code"]["source_refs"]
    assert _repository_digest(repository) == tree_before
    assert _repository_digest(repository, git_only=True) == git_before


@pytest.mark.asyncio
async def test_offline_agent_degrades_to_trace_only_ai_report() -> None:
    authority = SourceAnalysisAuthority(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        smartperfetto_state="completed",
        agent_id=AGENT_ID,
        workspace_id=WORKSPACE_ID,
        validation_profile_id=None,
        finding_hints=(),
        direct_identifiers=(),
        finding_ids=(),
        evidence_ids=(),
    )

    class Authority:
        async def load_source_authority(self, _analysis_id):
            return authority

    class OfflineTasks:
        async def context_status(self, **_kwargs):
            raise SourceTaskError

    class Scheduler:
        def __init__(self) -> None:
            self.calls = 0

        async def enqueue_once(self, **_kwargs):
            self.calls += 1

    states = InMemorySourceAnalysisStateRepository()
    scheduler = Scheduler()
    orchestrator = SourceOrchestrator(
        authority=Authority(),
        tasks=OfflineTasks(),
        artifacts=object(),
        states=states,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert await orchestrator.prepare_for_synthesis(ANALYSIS_ID) is True
    state = states.get(ANALYSIS_ID)
    unavailable = {
        **_source_not_requested(),
        "requested": True,
        "provider_kind": "agent_workspace",
        "agent_id": str(AGENT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "snapshot_policy": "tracked_worktree",
        "context_state": state.context_state,
    }
    report, provider, projection = await _report(
        source_context=None,
        source_document=unavailable,
    )

    assert scheduler.calls == 1
    assert state.failure_code == "source_agent_unavailable"
    assert provider.calls == 1
    assert projection["source_context"] is None
    assert report["synthesis"]["state"] == "completed"
    assert report["source_code"]["context_state"] == "unavailable"
    assert report["state"] == "partially_completed"
