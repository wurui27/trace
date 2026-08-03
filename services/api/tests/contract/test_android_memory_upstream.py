from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from perfpilot_api.engines.android_memory_contracts import AndroidMemoryContext
from perfpilot_api.engines.android_memory_worker import LocalAndroidMemoryWorker


_PINNED_COMMIT = "d5514972ced78c3faa7fc17589c1ea9231645056"


class _ContractStage:
    def __init__(self, input_dir: Path) -> None:
        self.input_dir = input_dir
        self.cleanup_calls = 0

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


async def _git_head(repository_root: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-C",
        str(repository_root),
        "rev-parse",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await process.communicate()
    assert process.returncode == 0
    return stdout.decode("ascii").strip()


async def _wait_for_terminal(worker: LocalAndroidMemoryWorker, run_id: str) -> str:
    while True:
        state = await worker.status(run_id)
        if state != "running":
            return state
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_pinned_upstream_strict_minimal_meminfo_contract(tmp_path: Path) -> None:
    configured_root = os.environ.get("PERFPILOT_ANDROID_MEMORY_ROOT")
    if configured_root is None:
        pytest.skip("PERFPILOT_ANDROID_MEMORY_ROOT is not configured")

    repository_root = Path(configured_root)
    assert repository_root.is_absolute()
    assert await _git_head(repository_root) == _PINNED_COMMIT

    input_dir = tmp_path / "input"
    meminfo_dir = input_dir / "meminfo"
    meminfo_dir.mkdir(parents=True)
    fixture = Path(__file__).parents[1] / "fixtures" / "android_memory" / "minimal_meminfo.txt"
    shutil.copyfile(fixture, meminfo_dir / "minimal_meminfo.txt")

    stage = _ContractStage(input_dir)
    worker = LocalAndroidMemoryWorker(
        python_binary=Path(sys.executable),
        repository_root=repository_root,
        run_root=tmp_path / "runs",
        runtime_commit=_PINNED_COMMIT,
        max_output_bytes=32 * 1024 * 1024,
    )
    run_id = "memory-11111111111111111111111111111111"
    try:
        await worker.start(
            run_id=run_id,
            staged=stage,  # type: ignore[arg-type]
            question=None,
            timeout_seconds=30,
        )
        state = await asyncio.wait_for(_wait_for_terminal(worker, run_id), timeout=30)
        result = await worker.result(run_id)
    finally:
        await worker.shutdown()

    assert state == "completed"
    assert result.exit_code in (0, 2)
    assert result.payload is not None
    context = AndroidMemoryContext.model_validate_json(result.payload)
    assert context.context_type == "android-memory-ai-context"
    assert context.schema_version == "1.2"
    assert context.generator.name == "android-memory-ai"
    assert context.generator.version == "1.2.0"
    assert (
        context.analysis_contract.support_level
        == context.analysis_contract.primary_intent_support_level
    )
    assert context.analysis_contract.privacy.raw_contents_embedded is False
    assert context.analysis_contract.privacy.local_paths_included is False
    assert str(repository_root).encode() not in result.payload
    assert str(tmp_path).encode() not in result.payload
    assert stage.cleanup_calls == 1
