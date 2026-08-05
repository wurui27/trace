from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

AGENT_ID = UUID("71000000-0000-4000-8000-000000000001")
OTHER_AGENT_ID = UUID("71000000-0000-4000-8000-000000000002")
EXECUTION_ID = UUID("73000000-0000-4000-8000-000000000001")
ANALYSIS_ID = UUID("30000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
DEVICE_DIGEST = "a" * 64
TASK_KID = "task-key-2026-08"


def _segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def task_claims() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "aud": "perfpilot-agent",
        "agent_id": str(AGENT_ID),
        "device_digest": DEVICE_DIGEST,
        "execution_id": str(EXECUTION_ID),
        "lease_version": 1,
        "analysis_id": str(ANALYSIS_ID),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=90)).isoformat(),
        "package_name": "dev.perfpilot.demo",
        "launch_activity": "dev.perfpilot.demo/dev.perfpilot.demo.MainActivity",
        "cleanup_policy": "uninstall",
        "input_artifacts": [
            {
                "artifact_id": str(ARTIFACT_ID),
                "kind": "apk",
                "mime": "application/vnd.android.package-archive",
                "size": 1_048_576,
                "sha256_b64": "A" * 43 + "=",
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
        "allowed_uploads": ["startup_trace", "agent_log"],
    }


@pytest.fixture
def sign_task(
    signing_key: Ed25519PrivateKey,
) -> Callable[[dict[str, Any], str], str]:
    def sign(claims: dict[str, Any], kid: str = TASK_KID) -> str:
        protected = _segment(_canonical({"alg": "EdDSA", "kid": kid, "typ": "perfpilot-task+jws"}))
        payload = _segment(_canonical(claims))
        signing_input = f"{protected}.{payload}".encode("ascii")
        signature = _segment(signing_key.sign(signing_input))
        return f"{protected}.{payload}.{signature}"

    return sign
