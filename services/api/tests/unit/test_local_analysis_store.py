from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from perfpilot_api.local_analysis_store import (
    LocalAnalysisStore,
    LocalAnalysisStoreError,
)


ANALYSIS_ID = UUID("91000000-0000-4000-8000-000000000001")


def test_store_round_trips_analysis_state_and_documents(tmp_path: Path) -> None:
    store = LocalAnalysisStore(tmp_path)
    state = {
        "schema_version": "1.0",
        "analysis_id": str(ANALYSIS_ID),
        "state": "analyzing",
    }
    report = {"schema_version": "1.1", "analysis_id": str(ANALYSIS_ID)}
    memory = {"schema_version": "1.2", "context_type": "android-memory-ai-context"}

    store.save_state(ANALYSIS_ID, state)
    store.save_document(ANALYSIS_ID, "report.json", report)
    store.save_document(ANALYSIS_ID, "android-memory-result.json", memory)

    assert store.load_states() == {ANALYSIS_ID: state}
    assert store.load_document(ANALYSIS_ID, "report.json") == report
    assert store.load_document(ANALYSIS_ID, "android-memory-result.json") == memory


def test_store_rejects_symlinked_analysis_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    analyses = tmp_path / "analyses"
    analyses.mkdir()
    (analyses / str(ANALYSIS_ID)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalAnalysisStoreError, match="unsafe local analysis path"):
        LocalAnalysisStore(tmp_path).save_state(
            ANALYSIS_ID,
            {"schema_version": "1.0"},
        )


def test_store_ignores_non_uuid_directories_but_rejects_corrupt_state(tmp_path: Path) -> None:
    analyses = tmp_path / "analyses"
    analyses.mkdir()
    (analyses / "notes").mkdir()
    analysis_directory = analyses / str(ANALYSIS_ID)
    analysis_directory.mkdir()
    (analysis_directory / "state.json").write_text("[]", encoding="utf-8")

    with pytest.raises(LocalAnalysisStoreError, match="invalid local analysis document"):
        LocalAnalysisStore(tmp_path).load_states()


def test_store_replaces_existing_state_without_leaving_temporary_files(
    tmp_path: Path,
) -> None:
    store = LocalAnalysisStore(tmp_path)
    store.save_state(ANALYSIS_ID, {"schema_version": "1.0", "version": 1})
    store.save_state(ANALYSIS_ID, {"schema_version": "1.0", "version": 2})

    analysis_directory = tmp_path / "analyses" / str(ANALYSIS_ID)
    assert json.loads((analysis_directory / "state.json").read_text()) == {
        "schema_version": "1.0",
        "version": 2,
    }
    assert sorted(path.name for path in analysis_directory.iterdir()) == ["state.json"]
