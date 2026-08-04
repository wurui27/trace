"""Load the immutable system prompt used for AI synthesis."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from importlib.resources import files


_PROMPT_RESOURCE = "perfpilot-synthesis-v1.txt"
_PROMPT_VERSION = "perfpilot-synthesis-v1"
_MAX_PROMPT_BYTES = 32 * 1024


class PromptLoadError(ValueError):
    def __init__(self, _detail: object = None) -> None:
        super().__init__("AI synthesis prompt is invalid")


@dataclass(frozen=True, slots=True)
class SynthesisPrompt:
    version: str
    sha256: str
    system_instruction: str
    raw_bytes: bytes = field(repr=False)


def load_synthesis_prompt() -> SynthesisPrompt:
    try:
        raw_bytes = files("perfpilot_api.ai.prompts").joinpath(_PROMPT_RESOURCE).read_bytes()
        system_instruction = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        raise PromptLoadError from None
    if not raw_bytes or len(raw_bytes) > _MAX_PROMPT_BYTES or not system_instruction.strip():
        raise PromptLoadError
    return SynthesisPrompt(
        version=_PROMPT_VERSION,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        system_instruction=system_instruction,
        raw_bytes=raw_bytes,
    )


__all__ = ["PromptLoadError", "SynthesisPrompt", "load_synthesis_prompt"]
