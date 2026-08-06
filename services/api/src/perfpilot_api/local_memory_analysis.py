"""Local bridge to the independently checked out Android Memory engine."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr

from perfpilot_api.engines.android_memory import AndroidMemoryAdapter
from perfpilot_api.engines.android_memory_contracts import (
    MemoryArtifactRef,
    MemoryCaptureManifest,
    MemorySubject,
)
from perfpilot_api.engines.android_memory_stager import AndroidMemoryStager
from perfpilot_api.engines.android_memory_worker import LocalAndroidMemoryWorker
from perfpilot_api.engines.contracts import (
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineStatus,
    SubmitConfig,
)


_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_CLAIM_ORIGIN = "https://local-memory.invalid"


class LocalMemoryAnalysisError(RuntimeError):
    def __init__(self, code: str = "android_memory_unavailable") -> None:
        super().__init__("Local Android memory analysis failed")
        self.code = code


class LocalMemoryAnalysisGateway(Protocol):
    engine_commit_sha: str

    async def analyze(
        self,
        *,
        analysis_id: UUID,
        evidence_path: Path,
        package_name: str,
        android_release: str | None,
        api_level: int | None,
    ) -> EngineResult: ...

    async def aclose(self) -> None: ...


class _MemoryAdapter(Protocol):
    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef: ...

    async def status(self, run_ref: EngineRunRef) -> EngineStatus: ...

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult: ...

    async def cancel(self, run_ref: EngineRunRef) -> object: ...


@dataclass(frozen=True, slots=True)
class _Claim:
    payload: bytes | Path
    size: int


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._payload


class _FileStream(httpx.AsyncByteStream):
    def __init__(self, path: Path) -> None:
        self._path = path

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        stream = self._path.open("rb")
        try:
            while True:
                chunk = await asyncio.to_thread(stream.read, 64 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(stream.close)


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _file_metadata(source: Path) -> tuple[Path, int, str]:
    if source.is_symlink():
        raise LocalMemoryAnalysisError("memory_evidence_invalid")
    try:
        path = source.resolve(strict=True)
        if not path.is_file():
            raise OSError
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise LocalMemoryAnalysisError("memory_evidence_invalid") from None
    if size < 1:
        raise LocalMemoryAnalysisError("memory_evidence_invalid")
    return path, size, base64.b64encode(digest.digest()).decode("ascii")


class LocalAndroidMemoryAnalysisGateway:
    def __init__(
        self,
        *,
        adapter_factory: Callable[[httpx.AsyncClient], _MemoryAdapter],
        shutdown: Callable[[], Awaitable[None]],
        engine_commit_sha: str,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if (
            _COMMIT.fullmatch(engine_commit_sha) is None
            or poll_interval_seconds < 0
        ):
            raise ValueError("local Android memory configuration is invalid")
        self.engine_commit_sha = engine_commit_sha
        self._claims: dict[str, _Claim] = {}
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._serve_claim),
            follow_redirects=False,
        )
        self._adapter = adapter_factory(self._client)
        self._shutdown = shutdown
        self._poll_interval_seconds = poll_interval_seconds
        self._closed = False

    async def _serve_claim(self, request: httpx.Request) -> httpx.Response:
        token = request.url.path.removeprefix("/v1/")
        claim = self._claims.get(token)
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.host != "local-memory.invalid"
            or request.url.path != f"/v1/{token}"
            or request.url.query
            or claim is None
        ):
            return httpx.Response(404, request=request)
        stream: httpx.AsyncByteStream = (
            _BytesStream(claim.payload)
            if isinstance(claim.payload, bytes)
            else _FileStream(claim.payload)
        )
        return httpx.Response(
            200,
            headers={"Content-Length": str(claim.size)},
            stream=stream,
            request=request,
        )

    def _claim(self, payload: bytes | Path, size: int) -> tuple[str, SecretStr]:
        token = secrets.token_urlsafe(32)
        self._claims[token] = _Claim(payload=payload, size=size)
        return token, SecretStr(f"{_CLAIM_ORIGIN}/v1/{token}")

    async def analyze(
        self,
        *,
        analysis_id: UUID,
        evidence_path: Path,
        package_name: str,
        android_release: str | None,
        api_level: int | None,
    ) -> EngineResult:
        if self._closed:
            raise LocalMemoryAnalysisError()
        evidence, evidence_size, evidence_sha = await asyncio.to_thread(
            _file_metadata,
            Path(evidence_path),
        )
        evidence_id = uuid4()
        manifest = MemoryCaptureManifest(
            schema_version="1.0",
            analysis_id=analysis_id,
            capture_id=uuid4(),
            phase="single",
            source="adb_agent",
            subject=MemorySubject(
                package=package_name,
                android_release=android_release or None,
                android_sdk=api_level,
            ),
            artifacts=(
                MemoryArtifactRef(
                    artifact_id=evidence_id,
                    role="handoff_archive",
                ),
            ),
        )
        manifest_payload = manifest.canonical_bytes()
        manifest_id = uuid4()
        manifest_token, manifest_url = self._claim(
            manifest_payload,
            len(manifest_payload),
        )
        evidence_token, evidence_url = self._claim(evidence, evidence_size)
        inputs = (
            EngineInput(
                artifact_id=manifest_id,
                kind="memory_capture_manifest",
                mime="application/json",
                size_bytes=len(manifest_payload),
                sha256_b64=_sha256_b64(manifest_payload),
                download_url=manifest_url,
            ),
            EngineInput(
                artifact_id=evidence_id,
                kind="memory_evidence",
                mime="application/x-tar",
                size_bytes=evidence_size,
                sha256_b64=evidence_sha,
                download_url=evidence_url,
            ),
        )
        run_ref: EngineRunRef | None = None
        try:
            try:
                run_ref = await self._adapter.submit(
                    inputs,
                    SubmitConfig(
                        execution_id=uuid4(),
                        analysis_id=analysis_id,
                        profile="auto",
                        question=None,
                        external_workspace_id=None,
                        timeout_seconds=900,
                    ),
                )
            finally:
                self._claims.pop(manifest_token, None)
                self._claims.pop(evidence_token, None)
            while True:
                status = await self._adapter.status(run_ref)
                if status.state in {"completed", "insufficient_data"}:
                    return await self._adapter.fetch_result(run_ref)
                if status.state in {"failed", "canceled", "awaiting_user"}:
                    raise LocalMemoryAnalysisError(
                        status.stable_error_code or "android_memory_failed"
                    )
                await asyncio.sleep(self._poll_interval_seconds)
        except asyncio.CancelledError:
            if run_ref is not None:
                try:
                    await asyncio.shield(self._adapter.cancel(run_ref))
                except Exception:
                    pass
            raise
        except LocalMemoryAnalysisError:
            raise
        except Exception:
            raise LocalMemoryAnalysisError() from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._shutdown()
        finally:
            await self._client.aclose()


def _repository_head(repository_root: Path, git_binary: Path) -> str:
    try:
        completed = subprocess.run(
            [str(git_binary), "-C", str(repository_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalMemoryAnalysisError() from None
    if completed.returncode != 0 or len(completed.stdout) > 128:
        raise LocalMemoryAnalysisError()
    commit = completed.stdout.decode("ascii", errors="ignore").strip()
    if _COMMIT.fullmatch(commit) is None:
        raise LocalMemoryAnalysisError()
    return commit


def build_local_memory_analysis_gateway(
    *,
    data_root: Path,
    checkout_root: Path | None = None,
) -> LocalAndroidMemoryAnalysisGateway:
    configured = checkout_root or Path(
        os.getenv(
            "PERFPILOT_LOCAL_ANDROID_MEMORY_ROOT",
            str(Path.home() / "Android-App-Memory-Analysis"),
        )
    )
    try:
        repository_root = Path(configured).expanduser().resolve(strict=True)
        python_binary = Path(sys.executable).resolve(strict=True)
        git_value = shutil.which("git")
        if (
            not repository_root.is_dir()
            or not (repository_root / "tools" / "ai_context.py").is_file()
            or git_value is None
        ):
            raise OSError
        git_binary = Path(git_value).resolve(strict=True)
    except OSError:
        raise LocalMemoryAnalysisError() from None
    commit = _repository_head(repository_root, git_binary)
    runtime_root = Path(data_root).resolve() / "android-memory"
    staging_root = runtime_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def resolve_commit(root: Path) -> str:
        return await asyncio.to_thread(_repository_head, root, git_binary)

    try:
        worker = LocalAndroidMemoryWorker(
            python_binary=python_binary,
            repository_root=repository_root,
            run_root=runtime_root / "worker",
            runtime_commit=commit,
            max_output_bytes=32 * 1024**2,
            commit_resolver=resolve_commit,
        )

        def adapter_factory(client: httpx.AsyncClient) -> AndroidMemoryAdapter:
            return AndroidMemoryAdapter(
                stager=AndroidMemoryStager(
                    client=client,
                    workspace_root=staging_root,
                    max_files=2048,
                    max_file_bytes=5 * 1024**3,
                    max_total_bytes=8 * 1024**3,
                ),
                worker=worker,
                max_timeout_seconds=900,
            )

        return LocalAndroidMemoryAnalysisGateway(
            adapter_factory=adapter_factory,
            shutdown=worker.shutdown,
            engine_commit_sha=commit,
        )
    except (OSError, ValueError):
        raise LocalMemoryAnalysisError() from None


__all__ = [
    "LocalAndroidMemoryAnalysisGateway",
    "LocalMemoryAnalysisError",
    "LocalMemoryAnalysisGateway",
    "build_local_memory_analysis_gateway",
]
