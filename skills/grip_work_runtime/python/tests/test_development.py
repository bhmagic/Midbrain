from __future__ import annotations

import json

import pytest

from grip_work_runtime.development import NumberedProfileStore, vector3


def test_numbered_profile_store_uses_lowest_available_number(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema": "test.profiles",
                "schema_version": 1,
                "default_profile_number": 1,
                "profiles": [
                    {"profile_number": 1, "name": "one", "value": 1},
                    {"profile_number": 3, "name": "three", "value": 3},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = NumberedProfileStore(
        path,
        expected_schema="test.profiles",
        validator=lambda payload: {"name": payload.get("name"), "value": int(payload["value"])},
    )

    saved = store.add({"name": "two", "value": 2})

    assert saved["profile_number"] == 2
    assert store.snapshot()["profiles"][-1]["name"] == "two"


def test_default_profile_cannot_be_deleted(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema": "test.profiles",
                "schema_version": 1,
                "default_profile_number": 1,
                "profiles": [
                    {"profile_number": 1, "name": "one"},
                    {"profile_number": 2, "name": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = NumberedProfileStore(
        path,
        expected_schema="test.profiles",
        validator=lambda payload: payload,
    )

    with pytest.raises(ValueError, match="default profile"):
        store.delete(1)


def test_vector3_rejects_zero_direction() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        vector3([0.0, 0.0, 0.0], "direction")
