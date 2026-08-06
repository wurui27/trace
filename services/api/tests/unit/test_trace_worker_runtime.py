from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from perfpilot_api.workers import trace_runtime
from perfpilot_api.workers.trace_runtime import (
    MountedSmartPerfettoCredentialResolver,
    TraceWorkerRuntime,
    TraceWorkerRuntimeError,
)


class FakeWorker:
    def __init__(self) -> None:
        self.run_once_calls = 0

    async def run_once(self) -> bool:
        self.run_once_calls += 1
        return True

    async def run_forever(self, stop: object = None) -> None:
        del stop


@pytest.mark.asyncio
async def test_mounted_credential_is_owner_only_reference_bound_and_redacted(
    tmp_path: Path,
) -> None:
    marker = "smartperfetto-runtime-secret-marker"
    secret_file = tmp_path / "smartperfetto-api-key"
    secret_file.write_text(f"{marker}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    reference = SecretStr("mounted://smartperfetto/service")

    resolver = MountedSmartPerfettoCredentialResolver(
        expected_reference=reference,
        path=secret_file,
    )

    resolved = await resolver(reference)
    assert resolved.get_secret_value() == marker
    assert marker not in repr(resolver)

    with pytest.raises(TraceWorkerRuntimeError, match="credential is unavailable") as captured:
        await resolver(SecretStr("mounted://smartperfetto/other"))
    assert marker not in str(captured.value)
    assert captured.value.__cause__ is None

    secret_file.chmod(0o644)
    with pytest.raises(TraceWorkerRuntimeError, match="credential is unavailable"):
        await resolver(reference)


@pytest.mark.asyncio
async def test_runtime_closes_every_owned_component_once_in_reverse_order() -> None:
    events: list[str] = []

    async def close_first() -> None:
        events.append("first")

    def close_second() -> None:
        events.append("second")

    runtime = TraceWorkerRuntime(
        worker=FakeWorker(),  # type: ignore[arg-type]
        close_callbacks=(close_first, close_second),
    )

    assert await runtime.run_once()
    await runtime.close()
    await runtime.close()

    assert events == ["second", "first"]


@pytest.mark.asyncio
async def test_runtime_cleanup_attempts_every_component_and_redacts_errors() -> None:
    events: list[str] = []
    marker = "cleanup-secret-marker"

    async def close_first() -> None:
        events.append("first")
        raise RuntimeError(marker)

    def close_second() -> None:
        events.append("second")

    runtime = TraceWorkerRuntime(
        worker=FakeWorker(),  # type: ignore[arg-type]
        close_callbacks=(close_first, close_second),
    )

    with pytest.raises(TraceWorkerRuntimeError, match="cleanup failed") as captured:
        await runtime.close()

    assert events == ["second", "first"]
    assert marker not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("app_env", "smartperfetto_enabled", "android_memory_enabled", "message"),
    [
        ("test", True, True, "production environment"),
        ("production", False, True, "SmartPerfetto must be enabled"),
        ("production", True, False, "Android Memory must be enabled"),
    ],
)
@pytest.mark.asyncio
async def test_builder_rejects_nonproduction_or_disabled_smartperfetto(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    smartperfetto_enabled: bool,
    android_memory_enabled: bool,
    message: str,
) -> None:
    monkeypatch.setattr(
        trace_runtime,
        "get_settings",
        lambda: SimpleNamespace(
            app_env=app_env,
            smartperfetto_enabled=smartperfetto_enabled,
            android_memory_enabled=android_memory_enabled,
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        await trace_runtime.build_production_trace_worker()


@pytest.mark.asyncio
async def test_builder_composes_pinned_runtime_and_closes_separate_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    lock_calls: list[dict[str, object]] = []
    artifact_calls: list[dict[str, object]] = []
    engine_calls: list[dict[str, object]] = []
    client_calls: list[dict[str, object]] = []
    marker = "smartperfetto-runtime-secret-marker"

    secret_file = tmp_path / "smartperfetto-api-key"
    secret_file.write_text(marker, encoding="utf-8")
    secret_file.chmod(0o400)
    lock_file = tmp_path / "engine-lock.yaml"
    schema_file = tmp_path / "engine-lock.schema.json"
    lock_file.touch()
    schema_file.touch()

    settings = SimpleNamespace(
        app_env="production",
        smartperfetto_enabled=True,
        android_memory_enabled=True,
        android_memory_backend="oci",
        android_memory_image_reference="registry.invalid/memory@sha256:" + "d" * 64,
        android_memory_run_root=tmp_path / "android-memory",
        android_memory_container_runtime=Path("/usr/bin/docker"),
        android_memory_max_output_bytes=32 * 1024 * 1024,
        android_memory_pids_limit=128,
        android_memory_memory_bytes=8 * 1024**3,
        android_memory_cpu_limit=4.0,
        android_memory_tmpfs_bytes=1024**3,
        android_memory_max_files=2048,
        android_memory_max_file_bytes=5 * 1024**3,
        android_memory_max_total_bytes=8 * 1024**3,
        android_memory_timeout_seconds=900,
        ai_enabled=True,
        control_database_url=SecretStr(
            "postgresql+psycopg://control.example/db?sslmode=verify-full"
        ),
        smartperfetto_credential_reference=SecretStr(
            "mounted://smartperfetto/service"
        ),
        smartperfetto_connect_timeout_seconds=5.0,
        smartperfetto_read_timeout_seconds=30.0,
        smartperfetto_write_timeout_seconds=30.0,
        smartperfetto_pool_timeout_seconds=5.0,
    )

    class FakeControlEngine:
        async def dispose(self) -> None:
            events.append("control-engine")

    class FakeArtifactRuntime:
        upload_service = object()
        engine_result_sink = object()
        tenant_router = object()
        bucket_resolver = object()
        s3_client = object()

        async def close(self) -> None:
            events.append("artifact-runtime")

    class FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            self.name = f"http-client-{len(client_calls) + 1}"
            self.follow_redirects = bool(kwargs["follow_redirects"])
            client_calls.append(kwargs)

        async def aclose(self) -> None:
            events.append(self.name)

    class FakeMemoryWorker:
        isolation = "oci"

        def __init__(self, *, image_reference: str, **_: object) -> None:
            self.image_reference = image_reference

        async def shutdown(self) -> None:
            events.append("android-memory-worker")

    fake_engine = FakeControlEngine()
    fake_artifacts = FakeArtifactRuntime()
    fake_lock = SimpleNamespace(
        android_memory=SimpleNamespace(image_digest="sha256:" + "d" * 64)
    )
    fake_sessions = object()
    fake_engine_service = object()

    def load_lock(path: Path, **kwargs: object) -> object:
        lock_calls.append({"path": path, **kwargs})
        return fake_lock

    async def build_artifacts(**kwargs: object) -> FakeArtifactRuntime:
        artifact_calls.append(kwargs)
        return fake_artifacts

    def build_engine(**kwargs: object) -> object:
        engine_calls.append(kwargs)
        return fake_engine_service

    monkeypatch.setattr(trace_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(trace_runtime, "load_engine_lock", load_lock)
    monkeypatch.setattr(trace_runtime, "create_control_engine", lambda _: fake_engine)
    monkeypatch.setattr(
        trace_runtime,
        "create_control_session_factory",
        lambda _: fake_sessions,
    )
    monkeypatch.setattr(trace_runtime, "build_artifact_runtime", build_artifacts)
    monkeypatch.setattr(trace_runtime.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(trace_runtime, "OciAndroidMemoryWorker", FakeMemoryWorker)
    monkeypatch.setattr(
        trace_runtime,
        "build_smartperfetto_execution_service",
        build_engine,
    )
    monkeypatch.setenv("PERFPILOT_TRACE_WORKER_ID", "trace-worker-1")
    monkeypatch.setenv(
        "PERFPILOT_SMARTPERFETTO_CREDENTIAL_FILE",
        str(secret_file),
    )
    monkeypatch.setenv("PERFPILOT_ENGINE_LOCK_FILE", str(lock_file))
    monkeypatch.setenv("PERFPILOT_ENGINE_LOCK_SCHEMA_FILE", str(schema_file))

    runtime = await trace_runtime.build_production_trace_worker()

    assert lock_calls == [
        {
            "path": lock_file,
            "schema_path": schema_file,
            "require_image_digests": True,
        }
    ]
    assert artifact_calls == [
        {
            "settings": settings,
            "control_session_factory": fake_sessions,
            "include_local_apk_inspector": False,
        }
    ]
    assert len(client_calls) == 2
    assert all(call["follow_redirects"] is False for call in client_calls)
    assert all(call["trust_env"] is False for call in client_calls)
    assert engine_calls[0]["engine_lock"] is fake_lock
    assert engine_calls[0]["result_sink"] is fake_artifacts.engine_result_sink
    assert engine_calls[0]["engine_client"] is not engine_calls[0]["artifact_client"]
    additional_adapters = engine_calls[0]["additional_adapters"]
    assert len(additional_adapters) == 1
    assert additional_adapters[0].descriptor.engine_id == "android_memory"
    assert runtime.worker._service._trace_service._schedule_synthesis is True  # type: ignore[attr-defined]
    assert runtime.worker._service._memory_service._timeout_seconds == 900  # type: ignore[attr-defined]
    credential_resolver = engine_calls[0]["credential_resolver"]
    resolved = await credential_resolver(settings.smartperfetto_credential_reference)
    assert resolved.get_secret_value() == marker

    await runtime.close()
    assert events == [
        "android-memory-worker",
        "http-client-2",
        "http-client-1",
        "artifact-runtime",
        "control-engine",
    ]


@pytest.mark.asyncio
async def test_builder_rolls_back_partial_runtime_and_redacts_build_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = "runtime-build-secret-marker"
    events: list[str] = []
    secret_file = tmp_path / "smartperfetto-api-key"
    secret_file.write_text("a" * 32, encoding="utf-8")
    secret_file.chmod(0o600)
    lock_file = tmp_path / "engine-lock.yaml"
    schema_file = tmp_path / "engine-lock.schema.json"
    lock_file.touch()
    schema_file.touch()

    settings = SimpleNamespace(
        app_env="production",
        smartperfetto_enabled=True,
        android_memory_enabled=True,
        android_memory_backend="oci",
        android_memory_image_reference="registry.invalid/memory@sha256:" + "d" * 64,
        android_memory_run_root=tmp_path / "android-memory",
        control_database_url=SecretStr(
            "postgresql+psycopg://control.example/db?sslmode=verify-full"
        ),
        smartperfetto_credential_reference=SecretStr(
            "mounted://smartperfetto/service"
        ),
    )

    class FakeControlEngine:
        async def dispose(self) -> None:
            events.append("control-engine")

    async def fail_artifacts(**_: object) -> object:
        raise RuntimeError(marker)

    fake_engine = FakeControlEngine()
    monkeypatch.setattr(trace_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        trace_runtime,
        "load_engine_lock",
        lambda *args, **kwargs: SimpleNamespace(
            android_memory=SimpleNamespace(image_digest="sha256:" + "d" * 64)
        ),
    )
    monkeypatch.setattr(trace_runtime, "create_control_engine", lambda _: fake_engine)
    monkeypatch.setattr(
        trace_runtime,
        "create_control_session_factory",
        lambda _: object(),
    )
    monkeypatch.setattr(trace_runtime, "build_artifact_runtime", fail_artifacts)
    monkeypatch.setenv("PERFPILOT_TRACE_WORKER_ID", "trace-worker-1")
    monkeypatch.setenv(
        "PERFPILOT_SMARTPERFETTO_CREDENTIAL_FILE",
        str(secret_file),
    )
    monkeypatch.setenv("PERFPILOT_ENGINE_LOCK_FILE", str(lock_file))
    monkeypatch.setenv("PERFPILOT_ENGINE_LOCK_SCHEMA_FILE", str(schema_file))

    with pytest.raises(
        TraceWorkerRuntimeError,
        match="^trace worker runtime is unavailable$",
    ) as captured:
        await trace_runtime.build_production_trace_worker()

    assert events == ["control-engine"]
    assert marker not in str(captured.value)
    assert captured.value.__cause__ is None


def test_trace_worker_console_script_is_registered() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["perfpilot-trace-worker"] == (
        "perfpilot_api.workers.trace_runtime:main"
    )
