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
TEAM_ID = UUID("82000000-0000-4000-8000-000000000001")
OTHER_TEAM_ID = UUID("82000000-0000-4000-8000-000000000002")


def test_store_round_trips_team_scoped_analysis_state_and_documents(
    tmp_path: Path,
) -> None:
    store = LocalAnalysisStore(tmp_path)
    state = {
        "schema_version": "1.0",
        "team_id": str(TEAM_ID),
        "analysis_id": str(ANALYSIS_ID),
        "state": "analyzing",
    }
    report = {"schema_version": "1.1", "analysis_id": str(ANALYSIS_ID)}
    memory = {"schema_version": "1.2", "context_type": "android-memory-ai-context"}

    store.save_state(TEAM_ID, ANALYSIS_ID, state)
    store.save_document(TEAM_ID, ANALYSIS_ID, "report.json", report)
    store.save_document(TEAM_ID, ANALYSIS_ID, "android-memory-result.json", memory)

    assert store.load_states() == {(TEAM_ID, ANALYSIS_ID): state}
    assert store.load_document(TEAM_ID, ANALYSIS_ID, "report.json") == report
    assert (
        store.load_document(TEAM_ID, ANALYSIS_ID, "android-memory-result.json")
        == memory
    )
    expected_root = tmp_path / "teams" / str(TEAM_ID) / "analyses" / str(ANALYSIS_ID)
    assert (expected_root / "state.json").is_file()
    assert not (tmp_path / "analyses").exists()
    assert not (tmp_path / str(ANALYSIS_ID)).exists()


def test_store_rejects_symlinked_analysis_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    analyses = tmp_path / "teams" / str(TEAM_ID) / "analyses"
    analyses.mkdir(parents=True)
    (analyses / str(ANALYSIS_ID)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalAnalysisStoreError, match="unsafe local analysis path"):
        LocalAnalysisStore(tmp_path).save_state(
            TEAM_ID,
            ANALYSIS_ID,
            {
                "schema_version": "1.0",
                "team_id": str(TEAM_ID),
                "analysis_id": str(ANALYSIS_ID),
            },
        )


def test_store_rejects_preexisting_symlinked_teams_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "teams").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalAnalysisStoreError, match="unsafe local analysis path"):
        LocalAnalysisStore(tmp_path)

    assert list(outside.iterdir()) == []


def test_store_rejects_teams_root_substitution_after_construction(
    tmp_path: Path,
) -> None:
    store = LocalAnalysisStore(tmp_path)
    original = tmp_path / "original-teams"
    (tmp_path / "teams").rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "teams").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalAnalysisStoreError, match="unsafe local analysis path"):
        store.save_state(
            TEAM_ID,
            ANALYSIS_ID,
            {
                "schema_version": "1.0",
                "team_id": str(TEAM_ID),
                "analysis_id": str(ANALYSIS_ID),
            },
        )

    assert list(outside.iterdir()) == []


def test_store_ignores_non_uuid_directories_but_rejects_corrupt_state(
    tmp_path: Path,
) -> None:
    analyses = tmp_path / "teams" / str(TEAM_ID) / "analyses"
    analyses.mkdir(parents=True)
    (analyses / "notes").mkdir()
    analysis_directory = analyses / str(ANALYSIS_ID)
    analysis_directory.mkdir()
    (analysis_directory / "state.json").write_text("[]", encoding="utf-8")

    with pytest.raises(
        LocalAnalysisStoreError, match="invalid local analysis document"
    ):
        LocalAnalysisStore(tmp_path).load_states()


def test_store_replaces_existing_state_without_leaving_temporary_files(
    tmp_path: Path,
) -> None:
    store = LocalAnalysisStore(tmp_path)
    store.save_state(
        TEAM_ID,
        ANALYSIS_ID,
        {
            "schema_version": "1.0",
            "team_id": str(TEAM_ID),
            "analysis_id": str(ANALYSIS_ID),
            "version": 1,
        },
    )
    store.save_state(
        TEAM_ID,
        ANALYSIS_ID,
        {
            "schema_version": "1.0",
            "team_id": str(TEAM_ID),
            "analysis_id": str(ANALYSIS_ID),
            "version": 2,
        },
    )

    analysis_directory = (
        tmp_path / "teams" / str(TEAM_ID) / "analyses" / str(ANALYSIS_ID)
    )
    assert json.loads((analysis_directory / "state.json").read_text()) == {
        "schema_version": "1.0",
        "team_id": str(TEAM_ID),
        "analysis_id": str(ANALYSIS_ID),
        "version": 2,
    }
    assert sorted(path.name for path in analysis_directory.iterdir()) == ["state.json"]


def test_store_isolates_same_analysis_id_between_teams(tmp_path: Path) -> None:
    store = LocalAnalysisStore(tmp_path)
    first = {"team_id": str(TEAM_ID), "analysis_id": str(ANALYSIS_ID), "owner": "first"}
    second = {
        "team_id": str(OTHER_TEAM_ID),
        "analysis_id": str(ANALYSIS_ID),
        "owner": "second",
    }

    store.save_state(TEAM_ID, ANALYSIS_ID, first)
    store.save_state(OTHER_TEAM_ID, ANALYSIS_ID, second)

    assert store.load_document(TEAM_ID, ANALYSIS_ID, "state.json") == first
    assert store.load_document(OTHER_TEAM_ID, ANALYSIS_ID, "state.json") == second
    assert store.load_states() == {
        (TEAM_ID, ANALYSIS_ID): first,
        (OTHER_TEAM_ID, ANALYSIS_ID): second,
    }


def test_store_rejects_state_copied_into_another_team_directory(tmp_path: Path) -> None:
    store = LocalAnalysisStore(tmp_path)
    state = {"team_id": str(TEAM_ID), "analysis_id": str(ANALYSIS_ID)}
    store.save_state(TEAM_ID, ANALYSIS_ID, state)
    source = tmp_path / "teams" / str(TEAM_ID) / "analyses" / str(ANALYSIS_ID)
    target = tmp_path / "teams" / str(OTHER_TEAM_ID) / "analyses" / str(ANALYSIS_ID)
    target.mkdir(parents=True)
    (target / "state.json").write_bytes((source / "state.json").read_bytes())

    with pytest.raises(
        LocalAnalysisStoreError, match="invalid local analysis document"
    ):
        store.load_states()


def test_store_ignores_legacy_global_analysis_layout(tmp_path: Path) -> None:
    legacy = tmp_path / "analyses" / str(ANALYSIS_ID)
    legacy.mkdir(parents=True)
    (legacy / "state.json").write_text(
        json.dumps({"analysis_id": str(ANALYSIS_ID), "state": "completed"}),
        encoding="utf-8",
    )

    assert LocalAnalysisStore(tmp_path).load_states() == {}
    assert (legacy / "state.json").is_file()
