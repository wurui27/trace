from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from perfpilot_api.security.agent_signatures import encode_ed25519_public_key
from perfpilot_api.security.task_snapshots import (
    TaskSnapshotRejected,
    TaskSnapshotSigner,
    snapshot_digest,
    verify_task_jws,
)

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000001")


def _claims(*, expires_at: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "agent_id": str(AGENT_ID),
        "device_digest": "a" * 64,
        "execution_id": str(EXECUTION_ID),
        "lease_version": 1,
        "analysis_id": str(ANALYSIS_ID),
        "issued_at": NOW.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(seconds=60)).isoformat(),
        "package_name": "com.example.perfpilot",
        "launch_activity": "com.example.perfpilot/com.example.perfpilot.MainActivity",
        "cleanup_policy": "uninstall",
        "input_artifacts": [
            {
                "artifact_id": str(ARTIFACT_ID),
                "kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": 4,
                "sha256_b64": base64.b64encode(b"a" * 32).decode("ascii"),
            }
        ],
        "scenarios": [
            {
                "scenario_type": "startup",
                "recipe_version": 1,
                "recipe_hash": "b" * 64,
                "duration_seconds": 15,
                "memory_rounds": 0,
                "swipe_count": 0,
            }
        ],
        "allowed_uploads": [
            "startup_trace",
            "scroll_trace",
            "memory_evidence",
            "agent_log",
        ],
    }


def test_signs_and_verifies_closed_eddsa_task_snapshot() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = TaskSnapshotSigner(private_key=private_key, kid="lan-test", clock=lambda: NOW)

    compact = signer.sign(_claims())
    verified = verify_task_jws(
        compact,
        encode_ed25519_public_key(private_key.public_key()),
        expected_kid="lan-test",
        now=NOW,
    )

    assert verified == _claims()
    assert snapshot_digest(compact) == snapshot_digest(compact)
    assert len(snapshot_digest(compact)) == 64
    assert compact not in repr(signer)


def test_rejects_tampering_and_snapshots_longer_than_ninety_seconds() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = TaskSnapshotSigner(private_key=private_key, kid="lan-test", clock=lambda: NOW)
    compact = signer.sign(_claims())
    header, payload, signature = compact.split(".")
    tampered = f"{header}.{payload[:-1]}A.{signature}"

    with pytest.raises(TaskSnapshotRejected):
        verify_task_jws(
            tampered,
            encode_ed25519_public_key(private_key.public_key()),
            now=NOW,
        )
    with pytest.raises(TaskSnapshotRejected):
        signer.sign(_claims(expires_at=NOW + timedelta(seconds=91)))
