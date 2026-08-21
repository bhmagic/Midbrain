from __future__ import annotations

import pytest

from sam2_scene_tracker.policy import parse_policy


def test_policy_requires_described_obstacle() -> None:
    with pytest.raises(ValueError, match="description"):
        parse_policy(
            {
                "contract_version": 1,
                "policy_id": "test",
                "objects": [
                    {"object_id": "table", "type": "KEEP_OUT", "description": ""}
                ],
            }
        )


def test_only_explicitly_described_objects_are_blocking() -> None:
    policy = parse_policy(
        {
            "contract_version": 1,
            "policy_id": "user-defined-scene",
            "objects": [
                {
                    "object_id": "table",
                    "type": "OBSTACLE",
                    "description": "the wooden support table itself",
                }
            ],
        }
    )

    assert [value.object_id for value in policy.blocking_objects] == ["table"]
    assert policy.as_dict()["unclaimed_visible_type"] == "PUSHABLE"
    assert policy.as_dict()["pushable_collision_policy"] == "IGNORE"


def test_policy_without_keep_out_objects_is_valid() -> None:
    policy = parse_policy(
        {
            "contract_version": 1,
            "policy_id": "location-only-scene",
            "objects": [
                {
                    "object_id": "roll",
                    "type": "WORK_OBJECT",
                    "description": "the toilet paper roll",
                }
            ],
        }
    )

    assert policy.blocking_objects == ()
    assert [value.object_id for value in policy.objects] == ["roll"]
