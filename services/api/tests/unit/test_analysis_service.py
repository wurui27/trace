from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest


TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("50000000-0000-4000-8000-000000000001")
APPLICATION_VERSION_ID = UUID("60000000-0000-4000-8000-000000000001")
INSPECTION_TOKEN = UUID("60000000-0000-4000-8000-000000000002")
CANDIDATE_ONE = UUID("70000000-0000-4000-8000-000000000001")
CANDIDATE_TWO = UUID("70000000-0000-4000-8000-000000000002")
CHILD_IDS = (
    UUID("80000000-0000-4000-8000-000000000001"),
    UUID("80000000-0000-4000-8000-000000000002"),
    UUID("80000000-0000-4000-8000-000000000003"),
)
RECIPE_IDS = (
    UUID("90000000-0000-4000-8000-000000000001"),
    UUID("90000000-0000-4000-8000-000000000002"),
    UUID("90000000-0000-4000-8000-000000000003"),
)
NOW = datetime(2026, 7, 28, 9, 10, 11, tzinfo=UTC)
SCENARIOS = ("cold_start", "scroll", "memory_cycle")
APK_MIME = "application/vnd.android.package-archive"
CHECKSUM = base64.b64encode(b"a" * 32).decode("ascii")
OTHER_CHECKSUM = base64.b64encode(b"b" * 32).decode("ascii")


@dataclass(frozen=True, slots=True)
class FakeInspectedApk:
    package_name: str = "com.example.perfpilot"
    version_name: str | None = "1.2.3"
    version_code: int = 123
    launch_activity: str | None = ".MainActivity"
    min_sdk: int | None = 28
    target_sdk: int | None = 35
    supported_abis: tuple[str, ...] = ("arm64-v8a",)
    has_native_libraries: bool = True
    manifest_sha256: str = "a" * 64


class FakeAnalysisRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.saved_request_hash: str | None = None
        self.reserve_error: Exception | None = None
        self.conflict_error: Exception | None = None
        self.reserve_calls: list[dict[str, object]] = []
        self.complete_calls: list[dict[str, object]] = []
        self.persist_calls: list[dict[str, object]] = []
        self.stage_calls: list[dict[str, object]] = []
        self.queue_calls: list[dict[str, object]] = []
        self.release_calls: list[dict[str, object]] = []
        self.fail_calls: list[dict[str, object]] = []
        self.prepared_scenarios = _prepared_scenarios()
        from perfpilot_api.services.analyses import FinalizationPreparation

        self.finalization_preparation = FinalizationPreparation(
            requirements=None,
            inspection_token=INSPECTION_TOKEN,
        )

    async def reserve_creation(self, **kwargs: object) -> Any:
        from perfpilot_api.services.analyses import (
            AnalysisIdempotencyConflictError,
            CreationReservation,
        )

        self.events.append("reserve")
        self.reserve_calls.append(kwargs)
        if self.reserve_error is not None:
            raise self.reserve_error
        request_hash = str(kwargs["request_hash"])
        if self.saved_request_hash is None:
            self.saved_request_hash = request_hash
        elif self.saved_request_hash != request_hash:
            if self.conflict_error is None:
                self.conflict_error = AnalysisIdempotencyConflictError("idempotency key was reused")
            raise self.conflict_error
        return CreationReservation(
            analysis_id=ANALYSIS_ID,
            state="creating",
            version=1,
        )

    async def ensure_tenant_parent(self, **_: object) -> str:
        self.events.append("ensure_tenant_parent")
        return "creating"

    async def mark_tenant_created(self, **_: object) -> None:
        self.events.append("mark_tenant_created")

    async def complete_creation(self, **kwargs: object) -> None:
        self.events.append("complete_creation")
        self.complete_calls.append(kwargs)

    async def load_view(self, **_: object) -> Any:
        self.events.append("load_view")
        return _analysis_view()

    async def require_finalizable(self, **_: object) -> Any:
        self.events.append("require_finalizable")
        return self.finalization_preparation

    async def release_apk_inspection(self, **kwargs: object) -> None:
        self.events.append("release_apk_inspection")
        self.release_calls.append(kwargs)

    async def fail_apk_inspection(self, **kwargs: object) -> None:
        self.events.append("fail_apk_inspection")
        self.fail_calls.append(kwargs)

    async def persist_apk_metadata(self, **kwargs: object) -> UUID:
        self.events.append("persist_apk_metadata")
        self.persist_calls.append(kwargs)
        return APPLICATION_VERSION_ID

    async def stage_tenant_scenarios(self, **kwargs: object) -> tuple[Any, ...]:
        self.events.append("stage_tenant_scenarios")
        self.stage_calls.append(kwargs)
        return self.prepared_scenarios

    async def queue_control_scenarios(self, **kwargs: object) -> None:
        self.events.append("queue_control_scenarios")
        self.queue_calls.append(kwargs)


class FakeUploadService:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.create_calls: list[dict[str, object]] = []
        self.finalize_calls: list[dict[str, object]] = []

    async def create_slot(self, **kwargs: object) -> Any:
        self.events.append("create_apk_slot")
        self.create_calls.append(kwargs)
        return _upload_slot(state="pending")

    async def finalize(self, **kwargs: object) -> Any:
        self.events.append("storage_finalize")
        self.finalize_calls.append(kwargs)
        return _upload_slot(state="finalized")


class FakeApkInspector:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []
        self.result = FakeInspectedApk()
        self.failure: Exception | None = None

    async def inspect(self, **kwargs: object) -> FakeInspectedApk:
        self.events.append("inspect_apk_metadata")
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.result


def _upload_slot(*, state: str) -> Any:
    from perfpilot_api.services.uploads import UploadSlot

    finalized = state == "finalized"
    return UploadSlot(
        artifact_id=ARTIFACT_ID,
        upload_id=UPLOAD_ID,
        artifact_kind="apk",
        mime=APK_MIME,
        size=4,
        sha256_b64=CHECKSUM,
        state=state,
        expires_at=NOW + timedelta(days=30 if finalized else 0, minutes=15),
        finalized_at=NOW if finalized else None,
        required_headers={} if finalized else {"Content-Type": APK_MIME},
        put_url=None if finalized else "https://objects.example/signed-secret",
        object_key=f"raw/analyses/{ANALYSIS_ID}/inputs/apk/{UPLOAD_ID}",
        version_id="immutable-version" if finalized else None,
    )


def _analysis_view() -> Any:
    from perfpilot_api.services.analyses import AnalysisView, SampleVerdictCounts

    return AnalysisView(
        analysis_id=ANALYSIS_ID,
        team_id=TEAM_ID,
        analysis_mode="device",
        state="created",
        version=2,
        application_version_id=None,
        application_metadata=None,
        apk_upload=None,
        scenarios=(),
        sample_verdict_counts=SampleVerdictCounts(
            valid=0,
            invalid=0,
            pending=0,
            validation_error=0,
            total=0,
        ),
        active_lease=None,
        report_available=False,
        created_at=NOW,
        started_at=None,
        completed_at=None,
        failure_code=None,
    )


def _prepared_scenarios() -> tuple[Any, ...]:
    from perfpilot_api.services.analyses import PreparedScenario

    return tuple(
        PreparedScenario(
            scenario_job_id=child_id,
            scenario_type=scenario_type,
            scenario_recipe_id=recipe_id,
            recipe_version=1,
            recipe_hash=f"{index:064x}",
            recipe_snapshot={"scenario_type": scenario_type, "version": 1},
        )
        for index, (scenario_type, child_id, recipe_id) in enumerate(
            zip(SCENARIOS, CHILD_IDS, RECIPE_IDS, strict=True),
            start=1,
        )
    )


def _service(
    repository: FakeAnalysisRepository,
    upload_service: FakeUploadService,
    inspector: FakeApkInspector,
) -> Any:
    from perfpilot_api.services.analyses import AnalysisService

    candidates = iter((CANDIDATE_ONE, CANDIDATE_TWO))
    return AnalysisService(
        repository=repository,
        upload_service=upload_service,
        apk_inspector=inspector,
        clock=lambda: NOW,
        uuid_source=candidates.__next__,
    )


async def _create(service: Any, *, checksum: str = CHECKSUM) -> Any:
    return await service.create_device_analysis(
        team_id=TEAM_ID,
        requested_by_user_id=USER_ID,
        idempotency_key="analysis-device-1",
        scenarios=SCENARIOS,
        apk_mime=APK_MIME,
        apk_size=4,
        apk_sha256_b64=checksum,
    )


async def _finalize(service: Any) -> Any:
    return await service.finalize_device_upload(
        team_id=TEAM_ID,
        analysis_id=ANALYSIS_ID,
        upload_id=UPLOAD_ID,
        caller_sha256_b64=CHECKSUM,
        caller_size=4,
    )


@pytest.mark.asyncio
async def test_same_request_hash_replays_the_idempotent_creation_saga_in_order() -> None:
    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    uploads = FakeUploadService(events)
    service = _service(repository, uploads, FakeApkInspector(events))

    first = await _create(service)
    second = await _create(service)

    one_saga = [
        "reserve",
        "ensure_tenant_parent",
        "create_apk_slot",
        "mark_tenant_created",
        "complete_creation",
        "load_view",
    ]
    assert events == one_saga + one_saga
    assert [call["candidate_analysis_id"] for call in repository.reserve_calls] == [
        CANDIDATE_ONE,
        CANDIDATE_TWO,
    ]
    assert len({call["request_hash"] for call in repository.reserve_calls}) == 1
    assert [call["request_hash"] for call in repository.complete_calls] == [
        repository.saved_request_hash,
        repository.saved_request_hash,
    ]
    assert [call["idempotency_key"] for call in uploads.create_calls] == [
        "initial-apk",
        "initial-apk",
    ]
    assert first.analysis_id == second.analysis_id == ANALYSIS_ID
    assert first.apk_upload == second.apk_upload == _upload_slot(state="pending")


@pytest.mark.asyncio
async def test_changed_request_hash_preserves_the_repository_conflict() -> None:
    from perfpilot_api.services.analyses import (
        AnalysisIdempotencyConflictError,
        canonical_analysis_request_hash,
    )

    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    repository.saved_request_hash = canonical_analysis_request_hash(
        scenarios=SCENARIOS,
        apk_mime=APK_MIME,
        apk_size=4,
        apk_sha256_b64=CHECKSUM,
    )
    uploads = FakeUploadService(events)
    service = _service(repository, uploads, FakeApkInspector(events))

    with pytest.raises(AnalysisIdempotencyConflictError) as caught:
        await _create(service, checksum=OTHER_CHECKSUM)

    assert caught.value is repository.conflict_error
    assert events == ["reserve"]
    assert uploads.create_calls == []


@pytest.mark.asyncio
async def test_queue_limit_error_is_preserved_without_allocating_tenant_or_upload_state() -> None:
    from perfpilot_api.services.analyses import AnalysisQueueLimitError

    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    expected = AnalysisQueueLimitError("team queue limit reached")
    repository.reserve_error = expected
    uploads = FakeUploadService(events)
    service = _service(repository, uploads, FakeApkInspector(events))

    with pytest.raises(AnalysisQueueLimitError) as caught:
        await _create(service)

    assert caught.value is expected
    assert events == ["reserve"]
    assert uploads.create_calls == []


@pytest.mark.asyncio
async def test_finalize_inspects_exact_artifact_before_staging_and_queueing_three_scenarios() -> (
    None
):
    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    uploads = FakeUploadService(events)
    inspector = FakeApkInspector(events)

    result = await _finalize(_service(repository, uploads, inspector))

    assert result == _upload_slot(state="finalized")
    assert events == [
        "require_finalizable",
        "storage_finalize",
        "inspect_apk_metadata",
        "persist_apk_metadata",
        "stage_tenant_scenarios",
        "queue_control_scenarios",
        "release_apk_inspection",
    ]
    assert inspector.calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": ARTIFACT_ID,
            "apk_sha256_b64": CHECKSUM,
        }
    ]
    assert repository.persist_calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": ARTIFACT_ID,
            "apk_sha256_b64": CHECKSUM,
            "metadata": inspector.result,
            "inspection_token": INSPECTION_TOKEN,
            "now": NOW,
        }
    ]
    assert repository.stage_calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": ARTIFACT_ID,
            "now": NOW,
        }
    ]
    assert tuple(item.scenario_type for item in repository.prepared_scenarios) == SCENARIOS
    from perfpilot_api.services.analyses import SchedulingRequirements

    assert repository.queue_calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "artifact_id": ARTIFACT_ID,
            "scenarios": repository.prepared_scenarios,
            "requirements": SchedulingRequirements(
                min_api_level=inspector.result.min_sdk,
                supported_abis=inspector.result.supported_abis,
            ),
            "now": NOW,
        }
    ]


@pytest.mark.asyncio
async def test_finalize_retry_after_control_queue_failure_reuses_persisted_metadata() -> None:
    from perfpilot_api.services.analyses import (
        AnalysisUnavailableError,
        FinalizationPreparation,
        SchedulingRequirements,
    )

    events: list[str] = []

    class RecoverableQueueRepository(FakeAnalysisRepository):
        async def persist_apk_metadata(self, **kwargs: object) -> UUID:
            result = await super().persist_apk_metadata(**kwargs)
            self.finalization_preparation = FinalizationPreparation(
                requirements=SchedulingRequirements(
                    min_api_level=28,
                    supported_abis=("arm64-v8a",),
                ),
                inspection_token=None,
            )
            return result

        async def queue_control_scenarios(self, **kwargs: object) -> None:
            await super().queue_control_scenarios(**kwargs)
            if len(self.queue_calls) == 1:
                raise AnalysisUnavailableError("control queue is unavailable")

    repository = RecoverableQueueRepository(events)
    uploads = FakeUploadService(events)
    inspector = FakeApkInspector(events)
    service = _service(repository, uploads, inspector)

    with pytest.raises(AnalysisUnavailableError, match="control queue"):
        await _finalize(service)
    result = await _finalize(service)

    assert result == _upload_slot(state="finalized")
    assert len(inspector.calls) == 1
    assert len(repository.persist_calls) == 1
    assert len(repository.stage_calls) == 2
    assert len(repository.queue_calls) == 2
    assert repository.release_calls == []


@pytest.mark.asyncio
async def test_inspector_unavailable_after_storage_finalize_never_stages_or_queues() -> None:
    from perfpilot_api.services.analyses import ApkInspectionUnavailableError

    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    uploads = FakeUploadService(events)
    inspector = FakeApkInspector(events)
    inspector.failure = RuntimeError("storage-route-sensitive-detail")

    with pytest.raises(ApkInspectionUnavailableError) as caught:
        await _finalize(_service(repository, uploads, inspector))

    assert str(caught.value) == "APK inspection is unavailable"
    assert events == [
        "require_finalizable",
        "storage_finalize",
        "inspect_apk_metadata",
        "release_apk_inspection",
    ]
    assert len(uploads.finalize_calls) == 1
    assert repository.persist_calls == []
    assert repository.stage_calls == []
    assert repository.queue_calls == []


@pytest.mark.parametrize(
    "metadata",
    (
        replace(FakeInspectedApk(), package_name=f"a.{('b' * 199)}"),
        replace(FakeInspectedApk(), version_code=2**31),
        replace(FakeInspectedApk(), min_sdk=2**31),
        replace(FakeInspectedApk(), target_sdk=2**31),
    ),
)
@pytest.mark.asyncio
async def test_db_incompatible_apk_metadata_fails_deterministically(
    metadata: FakeInspectedApk,
) -> None:
    from perfpilot_api.services.analyses import ApkInspectionError

    events: list[str] = []
    repository = FakeAnalysisRepository(events)
    uploads = FakeUploadService(events)
    inspector = FakeApkInspector(events)
    inspector.result = metadata

    with pytest.raises(ApkInspectionError) as caught:
        await _finalize(_service(repository, uploads, inspector))

    assert caught.value.code == "apk_invalid"
    assert events == [
        "require_finalizable",
        "storage_finalize",
        "inspect_apk_metadata",
        "fail_apk_inspection",
    ]
    assert repository.fail_calls == [
        {
            "team_id": TEAM_ID,
            "analysis_id": ANALYSIS_ID,
            "failure_code": "apk_invalid",
            "inspection_token": INSPECTION_TOKEN,
            "now": NOW,
        }
    ]
