from __future__ import annotations

import asyncio

import pytest

from physical_agent_test.scene_segmentation_policy_publisher import (
    SceneSegmentationPolicyPublisher,
)


class _Fabric:
    def __init__(self) -> None:
        self.observations = []

    async def publish(self, observation):
        self.observations.append(observation)
        return {"accepted": True}


def test_publisher_preserves_only_explicit_described_scene_objects() -> None:
    fabric = _Fabric()
    publisher = SceneSegmentationPolicyPublisher(fabric)
    result = asyncio.run(
        publisher.publish_policy(
            policy_id="table-only",
            objects=[
                {
                    "object_id": "table",
                    "type": "KEEP_OUT",
                    "description": "the complete support table",
                }
            ],
            arm_description="the complete robot arm and gripper",
        )
    )

    assert result["blocking_objects"] == ["table"]
    assert result["unclaimed_visible_type"] == "PUSHABLE"
    observation = fabric.observations[0]
    assert observation["stream"] == "robot_arm.scene.segmentation_policy"
    assert observation["data"]["objects"] == [
        {
            "object_id": "table",
            "type": "KEEP_OUT",
            "description": "the complete support table",
        }
    ]


def test_repeating_same_explicit_map_request_starts_a_new_mapping_epoch() -> None:
    fabric = _Fabric()
    publisher = SceneSegmentationPolicyPublisher(fabric)
    objects = [
        {
            "object_id": "table",
            "type": "KEEP_OUT",
            "description": "the complete support table",
        }
    ]

    first = asyncio.run(
        publisher.publish_policy(
            policy_id="table-only",
            objects=objects,
            arm_description="the complete robot arm and gripper",
        )
    )
    second = asyncio.run(
        publisher.publish_policy(
            policy_id="table-only",
            objects=objects,
            arm_description="the complete robot arm and gripper",
        )
    )

    assert first["revision"] != second["revision"]
    assert (
        fabric.observations[0]["data"]["revision"]
        != fabric.observations[1]["data"]["revision"]
    )


def test_publisher_rejects_descriptionless_or_nonblocking_policy() -> None:
    publisher = SceneSegmentationPolicyPublisher(_Fabric())
    with pytest.raises(ValueError, match="requires a description"):
        asyncio.run(
            publisher.publish_policy(
                policy_id="invalid",
                objects=[
                    {
                        "object_id": "table",
                        "type": "KEEP_OUT",
                        "description": "",
                    }
                ],
                arm_description="the arm",
            )
        )
    with pytest.raises(ValueError, match="at least one KEEP_OUT"):
        asyncio.run(
            publisher.publish_policy(
                policy_id="invalid",
                objects=[
                    {
                        "object_id": "paper",
                        "type": "PUSHABLE",
                        "description": "a loose paper sheet",
                    }
                ],
                arm_description="the arm",
            )
        )


def test_explicit_policy_persists_and_restores_after_restart(tmp_path) -> None:
    state_path = tmp_path / "scene-policy.json"
    first_fabric = _Fabric()
    first = SceneSegmentationPolicyPublisher(
        first_fabric,
        state_path=state_path,
    )
    published = asyncio.run(
        first.publish_policy(
            policy_id="table-only",
            objects=[
                {
                    "object_id": "table",
                    "type": "KEEP_OUT",
                    "description": "the complete support table",
                }
            ],
            arm_description="the complete robot arm and gripper",
        )
    )
    second_fabric = _Fabric()
    second = SceneSegmentationPolicyPublisher(
        second_fabric,
        state_path=state_path,
    )

    restored = asyncio.run(second.restore_policy())

    assert published["restored"] is False
    assert state_path.is_file()
    assert restored["status"] == "PUBLISHED"
    assert restored["restored"] is True
    assert restored["blocking_objects"] == ["table"]
    assert second_fabric.observations[0]["data"]["policy_id"] == "table-only"


def test_missing_persisted_policy_does_not_create_a_default(tmp_path) -> None:
    publisher = SceneSegmentationPolicyPublisher(
        _Fabric(),
        state_path=tmp_path / "missing.json",
    )

    result = asyncio.run(publisher.restore_policy())

    assert result["status"] == "NO_PERSISTED_POLICY"
    assert result["restored"] is False
