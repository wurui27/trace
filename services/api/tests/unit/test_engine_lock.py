from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from perfpilot_api.engines import EngineLockError, load_engine_lock


ROOT = Path(__file__).parents[4]
LOCK_PATH = ROOT / "infra" / "engines" / "engine-lock.yaml"
SCHEMA_PATH = ROOT / "infra" / "engines" / "engine-lock.schema.json"


def test_checked_in_engine_lock_loads_approved_pins() -> None:
    lock = load_engine_lock(
        LOCK_PATH,
        schema_path=SCHEMA_PATH,
        require_image_digests=False,
    )

    assert lock.schema_version == "1.0"
    assert lock.smartperfetto.source == "https://github.com/Gracker/SmartPerfetto.git"
    assert lock.smartperfetto.ref == "v1.0.38"
    assert lock.smartperfetto.commit == "1508f99788bfcf18cc861e4bf4f8b472e84240c3"
    assert lock.smartperfetto.image_digest is None
    assert lock.smartperfetto.contract == "workspace-agent-v1"
    assert lock.android_memory.source == "https://github.com/Gracker/Android-App-Memory-Analysis.git"
    assert lock.android_memory.ref is None
    assert lock.android_memory.commit == "d5514972ced78c3faa7fc17589c1ea9231645056"
    assert lock.android_memory.image_digest is None
    assert lock.android_memory.contract == "android-memory-ai-context-1.2"


def test_production_engine_lock_rejects_missing_image_digests() -> None:
    with pytest.raises(EngineLockError, match="image digest"):
        load_engine_lock(
            LOCK_PATH,
            schema_path=SCHEMA_PATH,
            require_image_digests=True,
        )


def test_engine_lock_rejects_unknown_fields(tmp_path: Path) -> None:
    lock_path = tmp_path / "engine-lock.yaml"
    shutil.copy(LOCK_PATH, lock_path)
    lock_path.write_text(
        f"{lock_path.read_text()}\n    unexpected: value\n",
        encoding="utf-8",
    )

    with pytest.raises(EngineLockError, match="invalid"):
        load_engine_lock(
            lock_path,
            schema_path=SCHEMA_PATH,
            require_image_digests=False,
        )


def test_engine_lock_rejects_nested_duplicate_yaml_keys(tmp_path: Path) -> None:
    lock_path = tmp_path / "engine-lock.yaml"
    lock_path.write_text(
        LOCK_PATH.read_text(encoding="utf-8").replace(
            "    commit: d5514972ced78c3faa7fc17589c1ea9231645056",
            "    commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "    commit: d5514972ced78c3faa7fc17589c1ea9231645056",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(EngineLockError, match=r"^engine lock is invalid$"):
        load_engine_lock(
            lock_path,
            schema_path=SCHEMA_PATH,
            require_image_digests=False,
        )


def test_engine_lock_rejects_duplicate_keys_in_nested_schema_objects(tmp_path: Path) -> None:
    schema_path = tmp_path / "engine-lock.schema.json"
    schema_path.write_text(
        SCHEMA_PATH.read_text(encoding="utf-8").replace(
            '"const": "1.0"',
            '"const": "0.9",\n      "const": "1.0"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(EngineLockError, match=r"^engine lock is invalid$"):
        load_engine_lock(
            LOCK_PATH,
            schema_path=schema_path,
            require_image_digests=False,
        )


def test_engine_lock_rejects_yaml_merge_keys(tmp_path: Path) -> None:
    lock_path = tmp_path / "engine-lock.yaml"
    lock_path.write_text(
        LOCK_PATH.read_text(encoding="utf-8").replace(
            "  android_memory:\n",
            "  android_memory:\n"
            "    <<: {commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(EngineLockError, match=r"^engine lock is invalid$"):
        load_engine_lock(
            lock_path,
            schema_path=SCHEMA_PATH,
            require_image_digests=False,
        )


def test_engine_lock_accepts_valid_image_digests(tmp_path: Path) -> None:
    lock_path = tmp_path / "engine-lock.yaml"
    lock_path.write_text(
        """\
schema_version: "1.0"
engines:
  smartperfetto:
    source: https://github.com/Gracker/SmartPerfetto.git
    ref: v1.0.38
    commit: 1508f99788bfcf18cc861e4bf4f8b472e84240c3
    image_digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    api_contract: workspace-agent-v1
  android_memory:
    source: https://github.com/Gracker/Android-App-Memory-Analysis.git
    ref: null
    commit: d5514972ced78c3faa7fc17589c1ea9231645056
    image_digest: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    output_contract: android-memory-ai-context-1.2
""",
        encoding="utf-8",
    )

    lock = load_engine_lock(
        lock_path,
        schema_path=SCHEMA_PATH,
        require_image_digests=True,
    )

    assert lock.smartperfetto.image_digest == (
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert lock.android_memory.image_digest == (
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )


@pytest.mark.parametrize(
    "lock_text",
    [
        "not: [valid",
        "schema_version: '1.0'\nengines: []\n",
    ],
)
def test_engine_lock_fails_closed_for_malformed_content(tmp_path: Path, lock_text: str) -> None:
    lock_path = tmp_path / "engine-lock.yaml"
    lock_path.write_text(lock_text, encoding="utf-8")

    with pytest.raises(EngineLockError, match=r"^engine lock is invalid$"):
        load_engine_lock(
            lock_path,
            schema_path=SCHEMA_PATH,
            require_image_digests=False,
        )


def test_engine_lock_fails_closed_for_an_unresolvable_schema_reference(tmp_path: Path) -> None:
    schema_path = tmp_path / "engine-lock.schema.json"
    schema_path.write_text('{"$ref": "#/$defs/missing"}', encoding="utf-8")

    with pytest.raises(EngineLockError, match=r"^engine lock is invalid$"):
        load_engine_lock(
            LOCK_PATH,
            schema_path=schema_path,
            require_image_digests=False,
        )
