from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from perfpilot_agent.security import SourceFindingHint
from perfpilot_agent.source_rules import ANDROID_RULES, select_source_context
from perfpilot_agent.source_snapshot import SourceSnapshotter


WORKSPACE_ID = UUID("92000000-0000-4000-8000-000000000001")


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


def _snapshot(tmp_path: Path):
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    files = {
        "app/src/main/java/demo/Startup.kt": (
            "package demo\nclass Startup {\n  fun onCreate() {\n    Thread.sleep(10)\n  }\n}\n"
        ),
        "app/src/main/AndroidManifest.xml": (
            '<manifest package="demo"><application android:name=".Startup" /></manifest>\n'
        ),
        "app/src/main/java/demo/Other.kt": (
            "package demo\nclass Other { fun warm() = Unit }\n"
        ),
        "app/src/main/java/com/rivotek/mediacenter/PlaybackActivity.kt": (
            "package com.rivotek.mediacenter\n"
            "class PlaybackActivity {\n"
            "  fun preparePlayback() {\n"
            "    Thread.sleep(10)\n"
            "  }\n"
            "}\n"
        ),
        "app/src/main/java/com/rivotek/mediacenter/CommentOnly.kt": (
            "package com.rivotek.mediacenter\n"
            "import android.content.ContentProvider\n"
            "// Application.onCreate and Thread.sleep are documentation only.\n"
            "class CommentOnly\n"
        ),
        "library/src/main/java/com/example/Unrelated.kt": (
            "package com.example\nclass Unrelated { fun pause() = Thread.sleep(10) }\n"
        ),
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=PerfPilot Test",
        "-c",
        "user.email=perfpilot@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return SourceSnapshotter(cache_root=tmp_path / "cache").create(
        repo,
        WORKSPACE_ID,
        created_at=datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
    )


def test_rules_are_fixed_ranking_signals_only() -> None:
    assert tuple(rule.rule_id for rule in ANDROID_RULES) == (
        "android.startup.main_thread_io",
        "android.startup.eager_initialization",
        "android.ui.blocking_wait",
        "android.compose.unstable_recomposition",
        "android.memory.listener_leak",
        "android.memory.bitmap_retention",
    )


def test_context_ranks_direct_symbol_then_android_component_with_bounded_fragments(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    hint = SourceFindingHint(
        finding_id=UUID("85000000-0000-4000-8000-000000000001"),
        evidence_ids=(UUID("86000000-0000-4000-8000-000000000001"),),
        rule_id="startup.main_thread_blocked",
        symbol_hints=("demo.Startup.onCreate",),
    )

    context = select_source_context(snapshot, (hint,), max_files=12, max_bytes=98_304)

    assert [item.relative_path for item in context.fragments[:2]] == [
        "app/src/main/java/demo/Startup.kt",
        "app/src/main/AndroidManifest.xml",
    ]
    assert context.total_bytes <= 96 * 1024
    assert all(len(item.content.splitlines()) <= 160 for item in context.fragments)
    assert all(not Path(item.relative_path).is_absolute() for item in context.fragments)
    assert all("\\" not in item.relative_path for item in context.fragments)
    assert context.fragments[0].symbol == "demo.Startup.onCreate"
    assert "trace_symbol" in context.fragments[0].match_signals
    assert "android.ui.blocking_wait" in context.fragments[0].rule_ids
    assert context.fragments[0].snapshot_hash == snapshot.snapshot_hash
    assert context.fragments[0].document()["snapshot_hash"] == snapshot.snapshot_hash
    assert len({fragment.source_ref_id for fragment in context.fragments}) == len(
        context.fragments
    )


def test_context_has_stable_file_order_and_enforces_file_and_byte_limits(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)

    first = select_source_context(snapshot, (), max_files=2, max_bytes=80)
    second = select_source_context(snapshot, (), max_files=2, max_bytes=80)

    assert [item.relative_path for item in first.fragments] == [
        item.relative_path for item in second.fragments
    ]
    assert len(first.fragments) <= 2
    assert first.total_bytes <= 80
    assert first.truncated is True
    assert b"api_key" not in first.canonical_bytes


def test_package_hint_narrows_to_app_module_and_extracts_concrete_rule_symbol(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    hint = SourceFindingHint(
        finding_id=UUID("85000000-0000-4000-8000-000000000003"),
        evidence_ids=(UUID("86000000-0000-4000-8000-000000000003"),),
        rule_id="startup.main_thread_blocked",
        symbol_hints=("com.rivotek.mediacenter",),
    )

    context = select_source_context(snapshot, (hint,), max_files=12, max_bytes=98_304)

    first = context.fragments[0]
    assert first.relative_path == (
        "app/src/main/java/com/rivotek/mediacenter/PlaybackActivity.kt"
    )
    assert first.symbol == "com.rivotek.mediacenter.PlaybackActivity.preparePlayback"
    assert first.finding_ids == (hint.finding_id,)
    assert first.evidence_ids == hint.evidence_ids
    assert first.match_signals == ("android_component", "android_rule")
    assert "Thread.sleep" in first.content
    comment_only = next(
        item for item in context.fragments if item.relative_path.endswith("CommentOnly.kt")
    )
    assert comment_only.rule_ids == ()
    assert comment_only.match_signals == ("android_component",)
    assert comment_only.symbol is None


def test_direct_symbol_fragment_is_centered_on_a_late_match(tmp_path: Path) -> None:
    repo = tmp_path / "long-app"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    source = repo / "app/src/main/java/demo/LateStartup.kt"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package demo\n"
        + "".join(f"// filler {line}\n" for line in range(220))
        + "fun lateStartupTraceSection() = Thread.sleep(1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=PerfPilot Test",
        "-c",
        "user.email=perfpilot@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    snapshot = SourceSnapshotter(cache_root=tmp_path / "cache").create(
        repo,
        WORKSPACE_ID,
        created_at=datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
    )
    hint = SourceFindingHint(
        finding_id=UUID("85000000-0000-4000-8000-000000000002"),
        evidence_ids=(),
        rule_id="startup.late_trace",
        symbol_hints=("lateStartupTraceSection",),
    )

    context = select_source_context(snapshot, (hint,), max_files=12, max_bytes=98_304)

    assert context.fragments[0].start_line > 1
    assert "lateStartupTraceSection" in context.fragments[0].content
    assert len(context.fragments[0].content.splitlines()) <= 160
