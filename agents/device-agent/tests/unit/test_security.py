from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from perfpilot_agent.security import TaskRejected, TaskVerifier, VerifiedSourceTask

AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
TEAM_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_AGENT_ID = UUID("71000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
DEVICE_DIGEST = "a" * 64
TASK_KID = "task-key-2026-08"


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def verifier(signing_key: Ed25519PrivateKey) -> TaskVerifier:
    return TaskVerifier(
        public_key_b64=public_key_b64(signing_key),
        kid=TASK_KID,
        clock=lambda: NOW,
    )


def test_task_verifier_accepts_the_bound_agent_lease_and_device(
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    sign_task: Callable[[dict[str, Any], str], str],
) -> None:
    task = verifier(signing_key).verify(
        sign_task(task_claims),
        expected_agent_id=AGENT_ID,
        expected_lease_version=1,
        known_device_digests={DEVICE_DIGEST},
    )

    assert task.agent_id == AGENT_ID
    assert task.device_digest == DEVICE_DIGEST
    assert task.lease_version == 1


def test_task_verifier_requires_external_team_match_for_v11(
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    sign_task: Callable[[dict[str, Any], str], str],
) -> None:
    task_claims.update(
        {
            "schema_version": "1.1",
            "team_id": str(TEAM_ID),
            "scenarios": [
                task_claims["scenarios"][0],
                {
                    "scenario_type": "scroll",
                    "recipe_version": 1,
                    "recipe_hash": "c" * 64,
                    "duration_seconds": 30,
                    "memory_rounds": 0,
                    "swipe_count": 5,
                },
            ],
            "allowed_uploads": ["startup_trace", "scroll_trace", "agent_log"],
        }
    )
    task = verifier(signing_key).verify(
        sign_task(task_claims),
        expected_agent_id=AGENT_ID,
        expected_team_id=TEAM_ID,
        expected_lease_version=1,
        known_device_digests={DEVICE_DIGEST},
    )
    assert task.team_id == TEAM_ID

    with pytest.raises(TaskRejected):
        verifier(signing_key).verify(
            sign_task(task_claims),
            expected_agent_id=AGENT_ID,
            expected_team_id=UUID("10000000-0000-4000-8000-000000000002"),
            expected_lease_version=1,
            known_device_digests={DEVICE_DIGEST},
        )
    with pytest.raises(TaskRejected):
        verifier(signing_key).verify(
            sign_task(task_claims),
            expected_agent_id=AGENT_ID,
            expected_lease_version=1,
            known_device_digests={DEVICE_DIGEST},
        )


def test_task_verifier_rejects_memory_upload_for_v11_capture(
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    sign_task: Callable[[dict[str, Any], str], str],
) -> None:
    task_claims.update(
        {
            "schema_version": "1.1",
            "team_id": str(TEAM_ID),
            "scenarios": [
                task_claims["scenarios"][0],
                {
                    "scenario_type": "scroll",
                    "recipe_version": 1,
                    "recipe_hash": "c" * 64,
                    "duration_seconds": 30,
                    "memory_rounds": 0,
                    "swipe_count": 5,
                },
            ],
            "allowed_uploads": [
                "startup_trace",
                "scroll_trace",
                "memory_evidence",
                "agent_log",
            ],
        }
    )

    with pytest.raises(TaskRejected):
        verifier(signing_key).verify(
            sign_task(task_claims),
            expected_agent_id=AGENT_ID,
            expected_team_id=TEAM_ID,
            expected_lease_version=1,
            known_device_digests={DEVICE_DIGEST},
        )


@pytest.mark.parametrize(
    ("test_type", "launch_mode", "scenario_type", "package_name", "launch_activity"),
    [
        (
            "cold_start",
            "automatic",
            "startup",
            "com.rivotek.mediacenter",
            "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        ),
        ("hot_start", "manual", "startup", None, None),
        (
            "scroll",
            "manual",
            "scroll",
            "com.rivotek.mediacenter",
            "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        ),
    ],
)
def test_task_verifier_accepts_script_capture_v12(
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    test_type: str,
    launch_mode: str,
    scenario_type: str,
    package_name: str | None,
    launch_activity: str | None,
) -> None:
    task_claims.update(
        {
            "schema_version": "1.2",
            "team_id": str(TEAM_ID),
            "test_type": test_type,
            "launch_mode": launch_mode,
            "package_name": package_name,
            "launch_activity": launch_activity,
            "cleanup_policy": "keep_installed",
            "input_artifacts": [],
            "scenarios": [
                {
                    "scenario_type": scenario_type,
                    "recipe_version": 1,
                    "recipe_hash": "d" * 64,
                    "duration_seconds": 15,
                    "memory_rounds": 0,
                    "swipe_count": 0,
                }
            ],
            "allowed_uploads": [f"{scenario_type}_trace", "agent_log"],
        }
    )

    task = verifier(signing_key).verify_device(
        task_claims,
        base64.b64encode(
            signing_key.sign(
                json.dumps(
                    task_claims,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
        ).decode("ascii"),
        expected_agent_id=AGENT_ID,
        expected_team_id=TEAM_ID,
        known_device_digests={DEVICE_DIGEST},
    )

    assert task.test_type == test_type
    assert task.launch_mode == launch_mode
    assert task.input_artifacts == ()


@pytest.mark.parametrize(
    "updates",
    [
        {"test_type": "scroll", "launch_mode": "automatic"},
        {"test_type": "cold_start", "launch_mode": "automatic", "package_name": None},
        {
            "test_type": "hot_start",
            "launch_mode": "manual",
            "package_name": "com.rivotek.mediacenter",
        },
    ],
)
def test_task_verifier_rejects_open_or_incoherent_script_capture_v12(
    task_claims: dict[str, Any],
    updates: dict[str, object],
) -> None:
    claims = {
        **task_claims,
        "schema_version": "1.2",
        "team_id": str(TEAM_ID),
        "test_type": "cold_start",
        "launch_mode": "automatic",
        "package_name": "com.rivotek.mediacenter",
        "launch_activity": "com.rivotek.mediacenter/.shell.MediaCenterActivity",
        "cleanup_policy": "keep_installed",
        "input_artifacts": [],
        "scenarios": [task_claims["scenarios"][0]],
        "allowed_uploads": ["startup_trace", "agent_log"],
        **updates,
    }
    if updates.get("test_type") == "scroll":
        claims["scenarios"] = [
            {**task_claims["scenarios"][0], "scenario_type": "scroll"}
        ]
        claims["allowed_uploads"] = ["scroll_trace", "agent_log"]
    if updates.get("launch_mode") == "manual" and "launch_activity" not in updates:
        claims["launch_activity"] = None

    with pytest.raises(ValidationError):
        # Model validation is also the signed device-task boundary.
        from perfpilot_agent.security import TaskSnapshot

        TaskSnapshot.model_validate(claims)


@pytest.mark.parametrize("failure", ["wrong_agent", "expired"])
def test_task_rejects_wrong_agent_and_expired_signature(
    failure: str,
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    sign_task: Callable[[dict[str, Any], str], str],
) -> None:
    if failure == "wrong_agent":
        task_claims["agent_id"] = str(OTHER_AGENT_ID)
    else:
        task_claims["issued_at"] = (NOW - timedelta(seconds=80)).isoformat()
        task_claims["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()

    with pytest.raises(TaskRejected):
        verifier(signing_key).verify(
            sign_task(task_claims),
            expected_agent_id=AGENT_ID,
            expected_lease_version=1,
            known_device_digests={DEVICE_DIGEST},
        )


@pytest.mark.parametrize(
    "failure",
    ["kid", "lease", "device", "extra_claim", "numeric_timestamp"],
)
def test_task_rejects_unbound_or_open_claims(
    failure: str,
    signing_key: Ed25519PrivateKey,
    task_claims: dict[str, Any],
    sign_task: Callable[[dict[str, Any], str], str],
) -> None:
    kid = TASK_KID
    lease = 1
    digests = {DEVICE_DIGEST}
    if failure == "kid":
        kid = "unknown-key"
    elif failure == "lease":
        lease = 2
    elif failure == "device":
        digests = {"f" * 64}
    elif failure == "extra_claim":
        task_claims["arbitrary_adb_command"] = "shell rm -rf /"
    else:
        task_claims["expires_at"] = int(NOW.timestamp()) + 90

    with pytest.raises(TaskRejected):
        verifier(signing_key).verify(
            sign_task(task_claims, kid),
            expected_agent_id=AGENT_ID,
            expected_lease_version=lease,
            known_device_digests=digests,
        )


def test_source_task_verifier_accepts_only_closed_agent_bound_snapshot(
    signing_key: Ed25519PrivateKey,
) -> None:
    snapshot = {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "task_type": "source_context",
        "execution_id": "73000000-0000-4000-8000-000000000001",
        "analysis_id": "30000000-0000-4000-8000-000000000001",
        "team_id": "10000000-0000-4000-8000-000000000001",
        "agent_id": str(AGENT_ID),
        "workspace_id": "91000000-0000-4000-8000-000000000001",
        "snapshot_policy": "tracked_worktree",
        "validation_profile_id": None,
        "lease_version": 1,
        "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
        "finding_hints": [],
        "limits": {"max_findings": 3, "max_files": 12, "max_bytes": 98_304},
    }
    canonical = __import__("json").dumps(
        snapshot,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    signature = base64.b64encode(signing_key.sign(canonical)).decode("ascii")

    task = verifier(signing_key).verify_source(
        snapshot,
        signature,
        expected_agent_id=AGENT_ID,
        expected_team_id=UUID(snapshot["team_id"]),
    )

    assert isinstance(task, VerifiedSourceTask)
    assert task.workspace_id == UUID("91000000-0000-4000-8000-000000000001")
    with pytest.raises(TaskRejected):
        verifier(signing_key).verify_source(
            {**snapshot, "device_digest": DEVICE_DIGEST},
            signature,
            expected_agent_id=AGENT_ID,
            expected_team_id=UUID(snapshot["team_id"]),
        )
