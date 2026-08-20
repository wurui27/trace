from __future__ import annotations

import base64
import hashlib


def test_synthesis_prompt_is_versioned_hashed_and_constrains_untrusted_input() -> None:
    from perfpilot_api.ai.prompt import load_synthesis_prompt

    prompt = load_synthesis_prompt()

    assert prompt.version == "perfpilot-finding-report-v4"
    assert prompt.sha256_b64 == base64.b64encode(
        hashlib.sha256(prompt.raw_bytes).digest()
    ).decode("ascii")
    assert len(prompt.sha256_b64) == 44
    assert prompt.system_instruction == prompt.raw_bytes.decode("utf-8")
    assert 0 < len(prompt.raw_bytes) <= 32 * 1024
    instruction = prompt.system_instruction.casefold()
    assert "untrusted" in instruction
    assert "question" in instruction
    assert "do not create" in instruction
    assert "facts" in instruction
    assert "ids" in instruction
    assert "tools" in instruction
    assert "only" in instruction
    assert "synthesis" in instruction
    assert "three" in instruction
    assert "thirty-six" in instruction
    assert "strong" in instruction
    assert "diff" in instruction
    assert "concrete recommendation" in instruction
    assert "schema version 2.1" in instruction
    assert "claim_refs" in instruction
    assert "问题点" in prompt.system_instruction
    assert "为什么会有这个问题" in prompt.system_instruction
    assert "结合源码判断的根因" in prompt.system_instruction
    assert "修改建议" in prompt.system_instruction
    assert "修改仅供参考" in prompt.system_instruction
    assert "must not write measurement numbers" in instruction
    assert "confidence_ceiling" in instruction
