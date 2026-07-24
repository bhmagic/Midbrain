from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

from stationary_world_arm_alignment.config import load_skill_config
from stationary_world_arm_alignment.math3d import transform_payload
from stationary_world_arm_alignment.models import RunMode
from stationary_world_arm_alignment.skill import AlignmentSkill


class FakeKeeper:
    async def ensure_valid(self) -> None:
        return None

    def status(self) -> dict:
        return {"mode": "test"}


class FakeProgress:
    async def update(self, **_: object) -> None:
        return None


class FakeFabric:
    async def transform(self, **_: object) -> dict:
        return transform_payload(np.eye(4, dtype=np.float64))


def translated(x: float, y: float, z: float) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, 3] = [x, y, z]
    return result


def test_dim_dual_solver_uses_foundation_gripper() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.progress = FakeProgress()
    skill.fabric = FakeFabric()
    samples = {
        "base": {
            "vio": [translated(0.0, 0.0, 1.0) for _ in range(6)],
            "camera": [translated(0.0, 0.0, 1.0) for _ in range(6)],
        },
        "gripper": {
            "vio": [translated(0.25, 0.0, 1.0) for _ in range(4)],
            "camera": [translated(0.25, 0.0, 1.0) for _ in range(4)],
        },
    }
    base_from_tool = translated(0.2, 0.0, 0.0)
    frame = SimpleNamespace(
        camera_frame="camera",
        world_frame="vio",
        timestamp_us=100,
        session_epoch="epoch",
        frame_number=1,
        calibration_revision="calibration",
    )

    result = asyncio.run(
        skill._finish_foundation_dual(
            samples=samples,
            validations=[{"attempt": 1, "accepted": True}],
            frame=frame,
            vio_beak=np.asarray([0.25, 0.0, 1.0]),
            base_from_tool=base_from_tool,
            alignment_id="alignment",
            skill_id="skill",
            vlm={"test": True},
            arm_is_home=False,
            keeper=FakeKeeper(),
        )
    )

    assert result["mode"] == str(RunMode.FOUNDATION_BASE_GRIPPER)
    assert result["schema_version"] == 2
    assert result["mode_contract"] == {
        "base_alignment_source": "FOUNDATIONPOSE_BASE_POSE",
        "gripper_alignment_source": "FOUNDATIONPOSE_GRIPPER_POSE",
        "foundation_pose_models": ["base", "gripper"],
        "requires_prior_alignment": False,
    }
    assert [
        measurement["source_type"]
        for measurement in result["gripper_measurements"]
    ] == [
        "FOUNDATIONPOSE_GRIPPER_POSE",
        "VLM_RGBD_BEAK",
    ]
    assert result["gripper_measurements"][0]["semantic_point"] == (
        "GRIPPER_MODEL_ORIGIN"
    )
    assert result["gripper_measurements"][1]["semantic_point"] == (
        "FOREMOST_BEAK_MEAN"
    )
    assert result["gripper_cross_source_comparison"]["applicable"] is True
    assert (
        result["gripper_cross_source_comparison"]["directly_comparable"]
        is False
    )
    assert result["diagnostics"]["gripper_samples"]["input_count"] == 4
    assert (
        result["diagnostics"]["symmetry"]["method"]
        == "FOUNDATION_GRIPPER_PLUS_ARM_KINEMATICS"
    )
