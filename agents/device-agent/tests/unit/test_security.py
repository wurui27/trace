from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import AGENT_ID, DEVICE_DIGEST, NOW, OTHER_AGENT_ID, TASK_KID
from perfpilot_agent.security import TaskRejected, TaskVerifier


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
