from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from stationary_world_arm_alignment.config import load_skill_config
from stationary_world_arm_alignment.foundation_engine import (
    PROVIDER_COMPATIBILITY_ROUTE,
)
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


class FakeStore:
    def __init__(self, values: dict[str, dict]) -> None:
        self.values = values

    def get(self, alignment_id: str) -> dict | None:
        return self.values.get(alignment_id)


def translated(x: float, y: float, z: float) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, 3] = [x, y, z]
    return result


def test_dim_dual_solver_uses_foundation_gripper() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.progress = FakeProgress()
    skill.base_pose_engine_route = PROVIDER_COMPATIBILITY_ROUTE
    skill.last_base_pose_engine_lifecycle = {
        "route": PROVIDER_COMPATIBILITY_ROUTE,
        "state": "TEST_CLEAN",
        "owned_session_count_after": 0,
        "gpu_resources_released": True,
        "backend_closed": True,
    }
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
            vio_from_camera=np.eye(4, dtype=np.float64),
        )
    )

    assert result["mode"] == str(RunMode.FOUNDATION_BASE_GRIPPER)
    assert result["schema_version"] == 3
    assert result["review_state"] == "CANDIDATE_REVIEW_REQUIRED"
    assert result["candidate_review_mode"] == "ENFORCED"
    assert result["motion_usable"] is False
    assert result["candidate"]["motion_usable"] is False
    assert result["candidate"]["frame_contract"]["camera_frame"] == "camera"
    assert np.allclose(
        result["candidate"]["transforms"]["world_from_camera"][
            "translation_m"
        ],
        [0.0, 0.0, 0.0],
    )
    assert result["candidate"]["expires_at_us"] > result["created_at_us"]
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


def test_vlm_only_candidate_inherits_finite_quality_lineage() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.base_pose_engine_route = PROVIDER_COMPATIBILITY_ROUTE
    skill.store = FakeStore(
        {
            "parent-null": {
                "alignment_id": "parent-null",
                "candidate": {
                    "candidate_id": "parent-null",
                    "confidence": None,
                    "bounded_error_estimate": {
                        "translation_m": None,
                        "rotation_rad": None,
                    },
                },
                "diagnostics": {
                    "parent_alignment_id": "parent-finite",
                },
            },
            "parent-finite": {
                "alignment_id": "parent-finite",
                "candidate": {
                    "candidate_id": "parent-finite",
                    "confidence": 0.82,
                    "bounded_error_estimate": {
                        "translation_m": 0.007,
                        "rotation_rad": 0.034,
                    },
                },
                "diagnostics": {},
            },
        }
    )
    frame = SimpleNamespace(
        camera_frame="camera",
        world_frame="vio",
        timestamp_us=100,
        session_epoch="epoch",
        frame_number=1,
        calibration_revision="calibration",
        observations={},
    )

    candidate = skill._calibration_candidate(
        alignment_id="child",
        mode=str(RunMode.VLM_GRIPPER_ONLY),
        created_at_us=100,
        expires_at_us=200,
        frame=frame,
        world_from_vio=np.eye(4, dtype=np.float64),
        world_from_base=np.eye(4, dtype=np.float64),
        vio_from_camera_reference=np.eye(4, dtype=np.float64),
        diagnostics={
            "parent_alignment_id": "parent-null",
            "vlm": {
                "gripper": {
                    "confidence": 0.97,
                }
            },
            "vlm_consensus": {
                "used": False,
                "inference_count": 1,
            },
        },
        review_mode="ENFORCED",
    )

    assert candidate["confidence"] == 0.82
    assert (
        candidate["bounded_error_estimate"]["translation_m"]
        == 0.01
    )
    assert (
        candidate["bounded_error_estimate"]["rotation_rad"]
        == 0.034
    )
    assert candidate["bounded_error_estimate"]["basis"] == (
        "ANCESTOR_ROTATION_AND_VLM_TRANSLATION_FLOOR"
    )
    assert candidate["quality_provenance"]["ancestor_alignment_id"] == (
        "parent-finite"
    )


def test_vlm_only_refinement_is_invariant_to_live_vio_drift() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.config["vlm_refine"]["consensus_trigger_m"] = 100.0
    skill.progress = FakeProgress()
    skill.base_pose_engine_route = PROVIDER_COMPATIBILITY_ROUTE
    skill.last_base_pose_engine_lifecycle = {
        "route": PROVIDER_COMPATIBILITY_ROUTE,
        "state": "NOT_STARTED",
        "owned_session_count_after": 0,
        "gpu_resources_released": True,
        "backend_closed": True,
    }
    prior = {
        "alignment_id": "prior-static-camera",
        "vio_session_epoch": "epoch",
        "world_from_vio": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "world_from_camera_reference": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "world_from_base": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "learned_tool_to_beak_translation_m": [0.0, 0.0, 0.0],
        "candidate": {
            "candidate_id": "prior-static-camera",
            "confidence": 0.9,
            "bounded_error_estimate": {
                "translation_m": 0.004,
                "rotation_rad": 0.02,
            },
            "camera_provenance": {
                "provider_id": "camera.test",
                "provider_instance_id": "camera-instance",
                "boot_id": "camera-boot",
                "calibration_revision": "calibration",
            },
            "frame_contract": {
                "camera_frame": "camera",
            },
        },
        "diagnostics": {},
    }
    skill.store = FakeStore({"prior-static-camera": prior})
    frame = SimpleNamespace(
        camera_frame="camera",
        world_frame="vio",
        timestamp_us=100,
        session_epoch="epoch",
        frame_number=1,
        calibration_revision="calibration",
        observations={
            "route": {
                "provider_id": "camera.test",
                "provider_instance_id": "camera-instance",
                "boot_id": "camera-boot",
            }
        },
    )
    live_vio_from_camera = translated(5.0, 6.0, 7.0)
    camera_beak = np.asarray([0.1, 0.2, 0.3], dtype=np.float64)
    vlm = {
        "gripper": {"confidence": 0.95},
    }

    with tempfile.TemporaryDirectory() as temporary:
        result = asyncio.run(
            skill._vlm_gripper_only(
                prior=prior,
                frame=frame,
                camera_beak=camera_beak,
                base_from_tool=np.eye(4, dtype=np.float64),
                alignment_id="refined-static-camera",
                skill_id="skill",
                vlm=vlm,
                keeper=FakeKeeper(),
                vision=object(),
                run_dir=Path(temporary),
                vio_from_camera=live_vio_from_camera,
            )
        )

    assert np.allclose(
        result["world_from_base"]["translation_m"],
        camera_beak,
    )
    assert np.allclose(
        result["world_from_camera_reference"]["translation_m"],
        [0.0, 0.0, 0.0],
    )


def test_vio_epoch_bridge_requires_exact_stationary_camera_identity() -> None:
    prior = {
        "candidate": {
            "camera_provenance": {
                "provider_id": "camera.fixed",
                "provider_instance_id": "camera-instance",
                "boot_id": "camera-boot",
                "calibration_revision": "camera-calibration",
            },
            "frame_contract": {
                "camera_frame": "camera-optical",
            },
        },
    }
    frame = SimpleNamespace(
        camera_frame="camera-optical",
        calibration_revision="camera-calibration",
        observations={
            "route": {
                "provider_id": "camera.fixed",
                "provider_instance_id": "camera-instance",
                "boot_id": "camera-boot",
            },
        },
    )

    assert AlignmentSkill._same_stationary_camera_identity(prior, frame)

    frame.observations["route"]["boot_id"] = "restarted-camera"
    assert not AlignmentSkill._same_stationary_camera_identity(prior, frame)
