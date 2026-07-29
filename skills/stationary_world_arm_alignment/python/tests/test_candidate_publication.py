from __future__ import annotations

import asyncio

from stationary_world_arm_alignment.skill import AlignmentSkill


class _Fabric:
    def __init__(self) -> None:
        self.observations = []

    async def publish_batch(self, observations) -> None:
        self.observations = list(observations)


def _result(mode: str) -> dict:
    transform = {
        "translation_m": [0.0, 0.0, 0.0],
        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
    }
    return {
        "schema": "midbrain.skill.stationary_world_arm_alignment.result",
        "skill_id": "skill",
        "alignment_id": "alignment",
        "world_frame": "world",
        "vio_world_frame": "vio",
        "vio_session_epoch": "epoch",
        "world_from_vio": transform,
        "world_from_base": transform,
        "candidate_review_mode": mode,
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "motion_usable": False,
        "expires_at_us": 999999,
    }


def _skill() -> AlignmentSkill:
    skill = object.__new__(AlignmentSkill)
    skill.sequence = 0
    skill.config = {"arm_base_frame": "arm_base"}
    skill.fabric = _Fabric()
    return skill


def test_shadow_candidate_keeps_tagged_legacy_transform_streams() -> None:
    skill = _skill()

    asyncio.run(skill._publish_result(_result("SHADOW")))

    streams = [item["stream"] for item in skill.fabric.observations]
    assert streams[:2] == [
        "transform.stationary_world.vio",
        "transform.stationary_world.arm_base",
    ]
    assert all(
        item["data"]["motion_usable"] is False
        for item in skill.fabric.observations[:2]
    )


def test_enforced_candidate_withholds_legacy_transform_streams() -> None:
    skill = _skill()

    asyncio.run(skill._publish_result(_result("ENFORCED")))

    streams = [item["stream"] for item in skill.fabric.observations]
    assert streams[:2] == [
        "transform.stationary_world.vio.candidate",
        "transform.stationary_world.arm_base.candidate",
    ]
    assert "transform.stationary_world.arm_base" not in streams
