from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from perfpilot_api.reports.smartperfetto_original import (
    SmartPerfettoOriginalInvalid,
    SmartPerfettoOriginalNotFound,
    persist_smartperfetto_original,
    read_smartperfetto_original,
)


def test_original_report_is_bound_to_team_analysis_size_hash_and_version(
    tmp_path: Path,
) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path,
        team_id=team_id,
        analysis_id=analysis_id,
        document={"summary": "原始结论", "findings": []},
    )

    payload = read_smartperfetto_original(
        root=tmp_path,
        binding=binding,
        team_id=team_id,
        analysis_id=analysis_id,
    )

    assert binding.mime == "application/json"
    assert binding.version == 1
    assert binding.size == len(payload)
    assert binding.sha256 == hashlib.sha256(payload).hexdigest()
    with pytest.raises(SmartPerfettoOriginalNotFound, match="^smartperfetto_original_not_found$"):
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=uuid4(),
            analysis_id=analysis_id,
        )


@pytest.mark.parametrize("mutation", ["content", "version", "symlink"])
def test_original_report_rejects_mutated_private_artifact(
    tmp_path: Path,
    mutation: str,
) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path,
        team_id=team_id,
        analysis_id=analysis_id,
        document={"summary": "原始结论"},
    )
    artifact = (
        tmp_path
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / f"smartperfetto-original-v{binding.version}.json"
    )
    if mutation == "content":
        artifact.write_bytes(artifact.read_bytes() + b" ")
    elif mutation == "version":
        object.__setattr__(binding, "version", 2)
    else:
        replacement = artifact.with_suffix(".replacement")
        replacement.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(replacement)

    with pytest.raises(SmartPerfettoOriginalInvalid, match="^smartperfetto_original_invalid$") as error:
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=team_id,
            analysis_id=analysis_id,
        )
    assert str(tmp_path) not in str(error.value)


def test_original_report_rejects_oversized_file(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path,
        team_id=team_id,
        analysis_id=analysis_id,
        document={"summary": "原始结论"},
    )
    artifact = (
        tmp_path
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / "smartperfetto-original-v1.json"
    )
    artifact.write_bytes(b"x" * 33)

    with pytest.raises(SmartPerfettoOriginalInvalid):
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=team_id,
            analysis_id=analysis_id,
            maximum_bytes=32,
        )


def test_original_report_preserves_valid_noncanonical_json_bytes(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    payload = b'{ "findings": [ ], "summary": "\\u539f\\u59cb" }\n'

    binding = persist_smartperfetto_original(
        root=tmp_path,
        team_id=team_id,
        analysis_id=analysis_id,
        payload=payload,
    )

    assert binding.size == len(payload)
    assert binding.sha256 == hashlib.sha256(payload).hexdigest()
    assert read_smartperfetto_original(
        root=tmp_path,
        binding=binding,
        team_id=team_id,
        analysis_id=analysis_id,
    ) == payload
