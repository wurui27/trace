from __future__ import annotations

import base64
import hashlib
from uuid import UUID


def test_synthesis_request_fingerprint_uses_only_approved_nonsecret_fields() -> None:
    from perfpilot_api.services.synthesis_executions import synthesis_request_fingerprint

    checksum = base64.b64encode(hashlib.sha256(b"canonical").digest()).decode("ascii")
    first = synthesis_request_fingerprint(
        canonical_sha256_b64=checksum,
        tenant_resource_version=7,
        question=" Why did scrolling jank? ",
        normalizer_version="smartperfetto-v1",
        prompt_template_version="perfpilot-synthesis-v1",
        prompt_template_sha256_b64=checksum,
        report_worker_image_digest="sha256:" + "a" * 64,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="openai",
        model="gpt-test",
        inference_config_hash="b" * 64,
        generation=1,
    )
    replay = synthesis_request_fingerprint(
        canonical_sha256_b64=checksum,
        tenant_resource_version=7,
        question="Why did scrolling jank?",
        normalizer_version="smartperfetto-v1",
        prompt_template_version="perfpilot-synthesis-v1",
        prompt_template_sha256_b64=checksum,
        report_worker_image_digest="sha256:" + "a" * 64,
        provider_protocol="chat-completions-json-schema-v1",
        provider_name="openai",
        model="gpt-test",
        inference_config_hash="b" * 64,
        generation=1,
    )

    assert first == replay
    assert len(first) == 64


def test_engine_result_ready_identity_is_deterministic() -> None:
    from perfpilot_api.services.engine_executions import engine_result_ready_event_id

    execution_id = UUID("e3000000-0000-4000-8000-000000000001")

    assert engine_result_ready_event_id(execution_id) == engine_result_ready_event_id(execution_id)
