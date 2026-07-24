from __future__ import annotations

from stationary_world_arm_alignment.persistence import CalibrationStore


def test_store_keeps_revision_and_latest_pointer(tmp_path) -> None:
    store = CalibrationStore(tmp_path)
    result = {"alignment_id": "revision-1", "valid": True}
    path = store.save(result)
    assert path.name == "revision-1.json"
    assert store.latest() == result
    assert store.list() == [result]
