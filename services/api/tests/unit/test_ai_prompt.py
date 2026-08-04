from __future__ import annotations

import hashlib


def test_synthesis_prompt_is_versioned_hashed_and_constrains_untrusted_input() -> None:
    from perfpilot_api.ai.prompt import load_synthesis_prompt

    prompt = load_synthesis_prompt()

    assert prompt.version == "perfpilot-synthesis-v1"
    assert prompt.sha256 == hashlib.sha256(prompt.raw_bytes).hexdigest()
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
