from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from perfpilot_api.reports.smartperfetto_original import (
    MAX_SMARTPERFETTO_ORIGINAL_BYTES,
    SmartPerfettoOriginalBinding,
    SmartPerfettoOriginalInvalid,
    SmartPerfettoOriginalNotFound,
    persist_smartperfetto_original,
    read_smartperfetto_original,
    restore_smartperfetto_original,
)


HTML = (
    b'<!DOCTYPE html>\n<html><body data-order="b a">'
    b'\xe4\xb8\xad\\u6587</body></html>\n'
)


def _artifact(root: Path, team_id: object, analysis_id: object) -> Path:
    return (
        root
        / "teams"
        / str(team_id)
        / "analyses"
        / str(analysis_id)
        / "smartperfetto-original-v2.html"
    )


def test_native_html_is_persisted_byte_for_byte(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()

    binding = persist_smartperfetto_original(
        root=tmp_path,
        team_id=team_id,
        analysis_id=analysis_id,
        payload=HTML,
    )

    assert binding.mime == "text/html"
    assert binding.size == len(HTML)
    assert binding.sha256 == hashlib.sha256(HTML).hexdigest()
    assert _artifact(tmp_path, team_id, analysis_id).stat().st_mode & 0o777 == 0o600
    assert read_smartperfetto_original(
        root=tmp_path,
        binding=binding,
        team_id=team_id,
        analysis_id=analysis_id,
    ) == HTML


def test_exact_html_retry_is_idempotent(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    first = persist_smartperfetto_original(
        root=tmp_path, team_id=team_id, analysis_id=analysis_id, payload=HTML
    )
    second = persist_smartperfetto_original(
        root=tmp_path, team_id=team_id, analysis_id=analysis_id, payload=HTML
    )
    assert second == first


def test_html_binding_rejects_cross_analysis_read(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path, team_id=team_id, analysis_id=analysis_id, payload=HTML
    )
    with pytest.raises(SmartPerfettoOriginalNotFound):
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=team_id,
            analysis_id=uuid4(),
        )


def test_html_tamper_is_rejected(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path, team_id=team_id, analysis_id=analysis_id, payload=HTML
    )
    _artifact(tmp_path, team_id, analysis_id).write_bytes(HTML + b"tamper")
    os.chmod(_artifact(tmp_path, team_id, analysis_id), 0o600)
    with pytest.raises(SmartPerfettoOriginalInvalid):
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=team_id,
            analysis_id=analysis_id,
        )


def test_html_symlink_is_rejected(tmp_path: Path) -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = persist_smartperfetto_original(
        root=tmp_path, team_id=team_id, analysis_id=analysis_id, payload=HTML
    )
    artifact = _artifact(tmp_path, team_id, analysis_id)
    artifact.unlink()
    victim = tmp_path / "victim.html"
    victim.write_bytes(HTML)
    artifact.symlink_to(victim)
    with pytest.raises(SmartPerfettoOriginalInvalid):
        read_smartperfetto_original(
            root=tmp_path,
            binding=binding,
            team_id=team_id,
            analysis_id=analysis_id,
        )


@pytest.mark.parametrize(
    "payload",
    [b"{}", b"", b"not html", b"x" * (MAX_SMARTPERFETTO_ORIGINAL_BYTES + 1)],
)
def test_non_native_html_is_rejected(tmp_path: Path, payload: bytes) -> None:
    with pytest.raises(SmartPerfettoOriginalInvalid):
        persist_smartperfetto_original(
            root=tmp_path,
            team_id=uuid4(),
            analysis_id=uuid4(),
            payload=payload,
        )


def test_html_binding_is_closed_and_versioned() -> None:
    team_id = uuid4()
    analysis_id = uuid4()
    binding = SmartPerfettoOriginalBinding(
        artifact_id=uuid4(),
        team_id=team_id,
        analysis_id=analysis_id,
        version=2,
        mime="text/html",
        size=len(HTML),
        sha256=hashlib.sha256(HTML).hexdigest(),
    )
    assert restore_smartperfetto_original(binding.private_document()) == binding
    invalid = binding.private_document()
    invalid["private_path"] = "/private/report.html"
    with pytest.raises(SmartPerfettoOriginalInvalid):
        restore_smartperfetto_original(invalid)
