from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[4] / "scripts" / "reset-ubuntu-analysis-data.py"


def _load_reset_module():
    spec = importlib.util.spec_from_file_location("reset_ubuntu_analysis_data", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reset_remains_anchored_when_validated_parent_path_is_swapped(tmp_path: Path) -> None:
    reset = _load_reset_module()
    trusted_parent = tmp_path / "trusted"
    parked_parent = tmp_path / "parked"
    analysis_root = trusted_parent / "analysis"
    state_root = tmp_path / "persistent-state"
    analysis_root.mkdir(parents=True)
    state_root.mkdir()
    (analysis_root / "old-analysis").write_text("delete me", encoding="utf-8")

    def swap_validated_parent() -> None:
        trusted_parent.rename(parked_parent)
        replacement = trusted_parent / "analysis"
        replacement.mkdir(parents=True)
        (replacement / "outside-marker").write_text("must survive", encoding="utf-8")

    reset.reset_analysis_root(
        str(analysis_root),
        str(analysis_root),
        str(state_root),
        after_open=swap_validated_parent,
    )

    assert (trusted_parent / "analysis" / "outside-marker").read_text(encoding="utf-8") == (
        "must survive"
    )
    assert not (parked_parent / "analysis" / "old-analysis").exists()
    assert (parked_parent / "analysis").stat().st_mode & 0o777 == 0o700


def test_reset_rejects_root_entry_swap_without_deleting_replacement(tmp_path: Path) -> None:
    reset = _load_reset_module()
    parent = tmp_path / "parent"
    analysis_root = parent / "analysis"
    parked_root = parent / "parked-analysis"
    state_root = tmp_path / "persistent-state"
    analysis_root.mkdir(parents=True)
    state_root.mkdir()
    (analysis_root / "old-analysis").write_text("original", encoding="utf-8")

    def swap_validated_root() -> None:
        analysis_root.rename(parked_root)
        analysis_root.mkdir()
        (analysis_root / "outside-marker").write_text("must survive", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsafe analysis data directory"):
        reset.reset_analysis_root(
            str(analysis_root),
            str(analysis_root),
            str(state_root),
            after_open=swap_validated_root,
        )

    assert (analysis_root / "outside-marker").read_text(encoding="utf-8") == "must survive"
    assert (parked_root / "old-analysis").read_text(encoding="utf-8") == "original"
    assert not any(name.startswith(".analysis.reset-") for name in os.listdir(parent))
