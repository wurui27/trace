from __future__ import annotations

import base64
import hashlib
import importlib.util
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
from perfpilot_api.local_agent_store import LocalAgentStore
from perfpilot_api.local_control_store import LocalControlStore
from perfpilot_api.reports.contracts import canonical_json_bytes
from perfpilot_api.reports.normalizer import NormalizedTraceReport
from perfpilot_api.reports.projection import build_ai_projection
from perfpilot_api.reports.source_context import validate_source_context
from perfpilot_api.reports.smartperfetto_original import (
    SmartPerfettoOriginalNotFound,
    persist_smartperfetto_original,
    read_smartperfetto_original,
)
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
    return json.loads((ROOT / "contracts/v1/examples" / name).read_text(encoding="utf-8"))


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
        candidate["verdict"] = "启动关键路径被同步初始化阻塞。"
        candidate["executive_summary"] = "将同步查询移到首帧之后，再重复相同的冷启动场景。"
        candidate["conclusions"] = [
            {
                "finding_id": finding["finding_id"],
                "evidence_ids": finding["evidence_ids"],
                "source_ref_ids": [],
                "problem": "SmartPerfetto 发现启动关键路径存在阻塞。",
                "cause": "Trace 证据显示主线程同步等待覆盖了启动关键区间。",
                "source_root_cause": "当前没有足够源码证据定位具体实现。",
                "recommendation": "移除关键路径中的同步等待，并按相同场景复测。",
            }
            for scenario in projection.document["scenarios"]
            for finding in scenario["findings"]
            if finding["status"] in {"confirmed", "suspected"}
            and finding["evidence_ids"]
        ]
        for finding in candidate["top_findings"]:
            finding["user_impact"] = "首屏显示时间晚于现有目标。"
        for index, recommendation in enumerate(candidate["recommendations"]):
            recommendation["title"] = "延后同步查询" if index == 0 else "重复启动采集"
            recommendation["action"] = (
                "将查询移到首帧之后。" if index == 0 else "修改后采集相同的冷启动流程。"
            )
            recommendation["expected_effect"] = (
                "移除启动关键路径中的同步等待。" if index == 0 else "依据现有阈值确认启动指标。"
            )
        for item in candidate["retest_plan"]:
            item["steps"] = "使用相同流程采集五次冷启动。"
        candidate["source_fixes"] = []
        source_context = projection.document.get("source_context")
        if isinstance(source_context, dict) and source_context.get("match_summary") == "strong":
            source_ref = source_context["fragments"][0]
            relative_path = source_ref["relative_path"]
            matching_conclusion = next(
                item
                for item in candidate["conclusions"]
                if item["finding_id"] in source_ref["finding_ids"]
            )
            matching_conclusion["source_ref_ids"] = [source_ref["source_ref_id"]]
            matching_conclusion["source_root_cause"] = (
                "源码中的启动方法在主线程同步读取设置，阻塞了首帧。"
            )
            candidate["source_fixes"] = [
                {
                    "fix_id": "96000000-0000-4000-8000-000000000001",
                    "finding_id": source_ref["finding_ids"][0],
                    "evidence_ids": source_ref["evidence_ids"],
                    "recommendation_priority": "p0",
                    "source_ref_ids": [source_ref["source_ref_id"]],
                    "rule_id": source_ref["rule_ids"][0],
                    "match_grade": "strong",
                    "relative_path": relative_path,
                    "symbol": source_ref["symbol"],
                    "diagnosis": "启动路径在主线程执行了同步等待。",
                    "diff": (
                        f"diff --git a/{relative_path} b/{relative_path}\n"
                        f"--- a/{relative_path}\n"
                        f"+++ b/{relative_path}\n"
                        "@@ -1 +1 @@\n"
                        "-fun onCreate() = Thread.sleep(42)\n"
                        "+fun onCreate() = launchAfterFirstFrame()\n"
                    ),
                    "validation_profile_id": None,
                    "retest_target": "重复冷启动并对比首帧耗时。",
                }
            ]
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
        "validation_profile_id": "94000000-0000-4000-8000-000000000001",
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
    assert report["source_code"]["fixes"][0]["relative_path"] == (
        "app/src/main/java/demo/Startup.kt"
    )
    assert report["source_code"]["fixes"][0]["diff"].startswith("diff --git a/app/")
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


@pytest.mark.asyncio
async def test_team_artifacts_reset_without_changing_persistent_users_or_agents(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    analysis_root = tmp_path / "analysis"
    control = LocalControlStore(state_root / "control")
    user_a = control.ensure_user("user01", "initial user password", False).principal
    user_b = control.ensure_user("user02", "initial user password", False).principal
    agents = LocalAgentStore(state_root / "agents", uuid_factory=lambda: AGENT_ID)
    await agents.create_pending(
        team_id=user_a.team_id,
        owner_user_id=user_a.user_id,
        name="Source Mac",
        registration_code_digest="a" * 64,
        registration_code_expires_at=NOW.replace(year=2027),
        now=NOW,
    )
    original_bytes = b"<!doctype html><html><body>SmartPerfetto original</body></html>\n"
    binding = persist_smartperfetto_original(
        root=analysis_root,
        team_id=user_a.team_id,
        analysis_id=ANALYSIS_ID,
        payload=original_bytes,
    )
    (analysis_root / "teams" / str(user_b.team_id) / "analyses").mkdir(parents=True)
    report, _provider, _projection = await _report(
        source_context=None,
        source_document=_source_not_requested(),
    )
    assert canonical_json_bytes(report) != original_bytes
    assert (
        read_smartperfetto_original(
            root=analysis_root,
            binding=binding,
            team_id=user_a.team_id,
            analysis_id=ANALYSIS_ID,
        )
        == original_bytes
    )
    with pytest.raises(SmartPerfettoOriginalNotFound):
        read_smartperfetto_original(
            root=analysis_root,
            binding=binding,
            team_id=user_b.team_id,
            analysis_id=ANALYSIS_ID,
        )

    control_bytes = (state_root / "control" / "control.json").read_bytes()
    agent_bytes = (state_root / "agents" / "agents.json").read_bytes()
    reset_script = ROOT / "scripts" / "reset-ubuntu-analysis-data.py"
    spec = importlib.util.spec_from_file_location("acceptance_reset", reset_script)
    assert spec is not None and spec.loader is not None
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    reset.reset_analysis_root(str(analysis_root), str(analysis_root), str(state_root))

    assert list(analysis_root.iterdir()) == []
    assert not any(".reset-" in path.name for path in tmp_path.iterdir())
    assert (state_root / "control" / "control.json").read_bytes() == control_bytes
    assert (state_root / "agents" / "agents.json").read_bytes() == agent_bytes
    reopened_control = LocalControlStore(state_root / "control")
    reopened_agents = LocalAgentStore(state_root / "agents")
    assert reopened_control.find_user("user01") == user_a
    assert reopened_control.find_user("user02") == user_b
    assert [agent.id for agent in await reopened_agents.list_team(user_a.team_id)] == [AGENT_ID]
