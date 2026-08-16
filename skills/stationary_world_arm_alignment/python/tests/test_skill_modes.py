from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from stationary_world_arm_alignment.config import load_skill_config
from stationary_world_arm_alignment.foundation_engine import (
    IN_PROCESS_EXECUTION_HOST,
    PROVIDER_COMPATIBILITY_ROUTE,
)
from stationary_world_arm_alignment.models import RunMode
import stationary_world_arm_alignment.skill as skill_module
from stationary_world_arm_alignment.skill import (
    AlignmentSkill,
    gripper_axis_depth_is_trusted,
)


class FakeKeeper:
    async def ensure_valid(self) -> None:
        return None

    def status(self) -> dict:
        return {"mode": "test"}


class FakeProgress:
    async def update(self, **_: object) -> None:
        return None


class FakeArtifacts:
    def __init__(self) -> None:
        self.overlays: list[bytes] = []

    async def set_overlay(self, payload: bytes) -> None:
        self.overlays.append(payload)


class FakeStore:
    def __init__(self, values: dict[str, dict]) -> None:
        self.values = values

    def get(self, alignment_id: str) -> dict | None:
        return self.values.get(alignment_id)


def translated(x: float, y: float, z: float) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, 3] = [x, y, z]
    return result


def test_low_confidence_gripper_depth_cannot_select_base_axis() -> None:
    assert not gripper_axis_depth_is_trusted({"confidence": 0.56}, 0.7)
    assert not gripper_axis_depth_is_trusted({"confidence": 0.699999}, 0.7)
    assert gripper_axis_depth_is_trusted({"confidence": 0.7}, 0.7)
    assert gripper_axis_depth_is_trusted({"confidence": 0.94}, 0.7)


def test_both_size_failures_return_closer_attempt_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.progress = FakeProgress()
    skill.artifacts = FakeArtifacts()
    skill.base_pose_engine_route = PROVIDER_COMPATIBILITY_ROUTE
    skill.foundation_pose_execution_host = IN_PROCESS_EXECUTION_HOST

    first_samples = {
        "base": {
            "camera": [translated(0.1, 0.0, 1.0) for _ in range(6)],
            "vio": [translated(0.1, 0.0, 1.0) for _ in range(6)],
        }
    }
    second_samples = {
        "base": {
            "camera": [translated(0.2, 0.0, 1.0) for _ in range(6)],
            "vio": [translated(0.2, 0.0, 1.0) for _ in range(6)],
        }
    }
    samples_by_attempt = {1: first_samples, 2: second_samples}
    axis_review_attempts: list[int] = []

    async def collect_foundation(**kwargs: object) -> tuple[dict, dict]:
        attempt = int(kwargs["attempt"])
        return samples_by_attempt[attempt], {
            "base": f"base-session-{attempt}"
        }

    def render_overlay(
        *_: object,
        attempt: int,
        **__: object,
    ) -> tuple[bytes, dict]:
        projected_size = 50.0 if attempt == 1 else 70.0
        return f"attempt-{attempt}".encode(), {
            "attempt": attempt,
            "positive_depth_corner_count": 8,
            "visible_corner_count": 8,
            "axis_origin_visible": True,
            "projected_box_xyxy_px": [
                0.0,
                0.0,
                projected_size,
                projected_size,
            ],
            "image_size_px": [100, 100],
        }

    async def publish_overlay(**kwargs: object) -> dict:
        return {
            "local_path": str(kwargs["overlay_path"]),
            "attempt": int(kwargs["attempt"]),
            "accepted": kwargs.get("accepted"),
        }

    async def select_and_apply_orientation(**kwargs: object) -> tuple[bytes, dict]:
        attempt = int(kwargs["attempt"])
        axis_review_attempts.append(attempt)
        assert kwargs["samples"] is second_samples
        return b"selected-attempt-2", {
            "axis_review": {
                "base_x_relation_to_gripper": "TOWARD_GRIPPER",
                "notes": "The red +X arrow points toward the gripper.",
            },
            "orientation_resolution": {
                "method": "SINGLE_DISCRETE_BASE_ORIENTATION_SELECTION",
                "base_x_relation_to_gripper": "TOWARD_GRIPPER",
                "selected_flip_deg": 0,
                "fitted_yaw_deg": 0.0,
                "yaw_correction_translation_norm_m": 0.0,
                "selected_orientation_correction_axis": "NONE",
                "orientation_correction_count": 0,
                "warning": None,
                "consistency_passed": True,
            },
            "base_up_alignment": {
                "status": "ALIGNED",
                "warning": None,
            },
            "projection": {
                "attempt": 2,
                "projected_box_xyxy_px": [0.0, 0.0, 70.0, 70.0],
            },
        }

    skill._collect_skill_local_foundation = collect_foundation
    skill._publish_pose_overlay = publish_overlay
    skill._select_and_apply_base_orientation = select_and_apply_orientation
    monkeypatch.setattr(skill_module, "render_pose_overlay", render_overlay)

    frame = SimpleNamespace(
        rgb=np.zeros((100, 100, 3), dtype=np.uint8),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0},
        camera_frame="camera",
    )
    returned_samples, validations = asyncio.run(
        skill._validated_foundation(
            skill_id="skill",
            alignment_id="alignment",
            run_dir=tmp_path,
            frame=frame,
            keeper=FakeKeeper(),
            vision=object(),
            own_sessions=[],
            include_gripper=False,
            base_visual_box_2d=[0, 0, 1000, 1000],
            vio_from_camera=np.eye(4, dtype=np.float64),
        )
    )

    assert returned_samples is second_samples
    assert axis_review_attempts == [2]
    assert len(validations) == 2
    assert validations[0]["accepted"] is False
    assert validations[0]["scale_review"]["mismatch_fraction"] == 0.5
    selected = validations[1]
    assert selected["selected_as_best_attempt"] is True
    assert selected["accepted"] is True
    assert selected["acceptance_mode"] == "BEST_OF_TWO_SIZE_WARNING"
    assert selected["scale_review"]["mismatch_fraction"] == pytest.approx(
        0.3
    )
    assert selected["axis_review"]["base_x_relation_to_gripper"] == (
        "TOWARD_GRIPPER"
    )
    assert any("retained as a warning" in value for value in selected["warnings"])


def test_rgbd_gripper_reference_owns_base_yaw_without_overlay_review() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    samples = {
        "base": {
            "camera": [translated(0.0, 0.0, 1.0) for _ in range(6)],
            "vio": [translated(0.0, 0.0, 1.0) for _ in range(6)],
        }
    }

    class NoOverlayReview:
        async def validate_base_pose(self, *_: object, **__: object) -> dict:
            raise AssertionError("RGB overlay review must not override RGB-D yaw")

    frame = SimpleNamespace(
        rgb=np.zeros((100, 100, 3), dtype=np.uint8),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0},
        camera_frame="camera",
    )
    _, selected = asyncio.run(
        skill._select_and_apply_base_orientation(
            samples=samples,
            vision=NoOverlayReview(),
            frame=frame,
            attempt=1,
            overlay=b"unused",
            mesh_minimum=np.array([-0.05, -0.05, 0.0]),
            mesh_maximum=np.array([0.05, 0.05, 0.1]),
            mesh_from_semantic=np.eye(4),
            axis_length_m=0.1,
            gripper_axis_reference={
                "available": True,
                "camera_system_xyz_m": [-0.25, 0.0, 1.2],
            },
            camera_system_up=np.array([0.0, 0.0, 1.0]),
        )
    )

    assert selected["axis_review"]["base_x_relation_to_gripper"] == (
        "AWAY_FROM_GRIPPER"
    )
    assert selected["orientation_resolution"]["selected_flip_deg"] == 180
    assert selected["orientation_resolution"]["orientation_correction_count"] == 1
    assert (
        selected["orientation_resolution"][
            "selected_orientation_correction_axis"
        ]
        == "Z"
    )
    assert selected["base_up_alignment"]["status"] == "ALIGNED"
    for basis in ("camera", "vio"):
        for transform in samples["base"][basis]:
            assert np.array_equal(transform[:3, 3], [0.0, 0.0, 1.0])
            assert np.array_equal(
                transform[:3, :3],
                np.diag([-1.0, -1.0, 1.0]),
            )


def test_untrusted_gripper_reference_uses_bounded_overlay_review() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    samples = {
        "base": {
            "camera": [translated(0.0, 0.0, 1.0) for _ in range(6)],
            "vio": [translated(0.0, 0.0, 1.0) for _ in range(6)],
        }
    }

    class OverlayReview:
        calls = 0

        async def validate_base_pose(self, *_: object, **__: object) -> dict:
            self.calls += 1
            return {"base_x_relation_to_gripper": "TOWARD_GRIPPER"}

    vision = OverlayReview()
    frame = SimpleNamespace(
        rgb=np.zeros((100, 100, 3), dtype=np.uint8),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0},
        camera_frame="camera",
    )
    _, selected = asyncio.run(
        skill._select_and_apply_base_orientation(
            samples=samples,
            vision=vision,
            frame=frame,
            attempt=1,
            overlay=b"reviewed-overlay",
            mesh_minimum=np.array([-0.05, -0.05, 0.0]),
            mesh_maximum=np.array([0.05, 0.05, 0.1]),
            mesh_from_semantic=np.eye(4),
            axis_length_m=0.1,
            gripper_axis_reference={
                "available": False,
                "warning": "Low-confidence depth mask was rejected.",
            },
            camera_system_up=np.array([0.0, 0.0, 1.0]),
        )
    )

    assert vision.calls == 1
    resolution = selected["orientation_resolution"]
    assert resolution["reference_source"] == "VLM_RGB_OVERLAY_FALLBACK"
    assert resolution["warning"] == "Low-confidence depth mask was rejected."
    assert resolution["orientation_correction_count"] == 0


def test_skill_applies_down_and_away_choice_at_mesh_center() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    mesh_from_base = np.eye(4, dtype=np.float64)
    mesh_from_base[2, 3] = -0.0446249945
    camera_from_mesh = np.eye(4, dtype=np.float64)
    camera_from_mesh[:3, :3] = np.diag([1.0, -1.0, -1.0])
    camera_from_mesh[:3, 3] = [0.0, 0.0, 1.0]
    raw_camera_from_base = camera_from_mesh @ mesh_from_base
    samples = {
        "base": {
            "camera": [raw_camera_from_base.copy() for _ in range(6)],
            "vio": [raw_camera_from_base.copy() for _ in range(6)],
        }
    }
    raw_gripper_in_base = np.array([-0.25, 0.0, -0.2, 1.0])
    camera_gripper = (raw_camera_from_base @ raw_gripper_in_base)[:3]

    class NoOverlayReview:
        async def validate_base_pose(self, *_: object, **__: object) -> dict:
            raise AssertionError("RGB-D must resolve this hypothesis")

    frame = SimpleNamespace(
        rgb=np.zeros((100, 100, 3), dtype=np.uint8),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0},
        camera_frame="camera",
    )
    _, selected = asyncio.run(
        skill._select_and_apply_base_orientation(
            samples=samples,
            vision=NoOverlayReview(),
            frame=frame,
            attempt=1,
            overlay=b"unused",
            mesh_minimum=np.array([-0.05, -0.05, -0.05]),
            mesh_maximum=np.array([0.05, 0.05, 0.05]),
            mesh_from_semantic=mesh_from_base,
            axis_length_m=0.1,
            gripper_axis_reference={
                "available": True,
                "camera_system_xyz_m": camera_gripper.tolist(),
            },
            camera_system_up=np.array([0.0, 0.0, 1.0]),
        )
    )

    resolution = selected["orientation_resolution"]
    assert resolution["selected_orientation_correction_axis"] == "Y"
    assert resolution["orientation_correction_count"] == 1
    assert resolution["mesh_center_translation_preserved"] is True
    assert resolution[
        "semantic_root_translation_adjustment_norm_m"
    ] == pytest.approx(0.089249989)
    for basis in ("camera", "vio"):
        for transform in samples["base"][basis]:
            assert np.allclose(transform[:3, :3], np.diag([-1.0, -1.0, 1.0]))
            assert transform[2, 3] == pytest.approx(1.0 - 0.0446249945)


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
            # Deliberately drift the VIO samples away from the timestamped
            # camera pose. A stationary-camera calibration must ignore this
            # drift when publishing the base transform.
            "vio": [translated(4.0, 5.0, 6.0) for _ in range(6)],
            "camera": [translated(0.0, 0.0, 1.0) for _ in range(6)],
        },
        "gripper": {
            "vio": [translated(1.25, 0.0, 0.0) for _ in range(4)],
            "camera": [translated(0.0, 0.0, 1.25) for _ in range(4)],
        },
    }
    frame = SimpleNamespace(
        camera_frame="camera",
        world_frame="vio",
        timestamp_us=100,
        session_epoch="epoch",
        frame_number=1,
        calibration_revision="calibration",
    )

    vio_from_camera = np.eye(4, dtype=np.float64)
    vio_from_camera[:3, :3] = np.array(
        [[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]]
    )
    result = asyncio.run(
        skill._finish_foundation_dual(
            samples=samples,
            validations=[
                    {
                        "attempt": 1,
                        "accepted": True,
                        "axis_review": {
                            "base_x_relation_to_gripper": "TOWARD_GRIPPER",
                        },
                        "base_up_alignment": {
                            "status": "ALIGNED",
                            "world_up_available": True,
                            "base_z_dot_world_up": 1.0,
                        },
                        "orientation_resolution": {
                        "method": "SINGLE_DISCRETE_BASE_ORIENTATION_SELECTION",
                        "base_x_relation_to_gripper": "TOWARD_GRIPPER",
                        "selected_flip_deg": 0,
                        "fitted_yaw_deg": 0.0,
                        "yaw_correction_translation_norm_m": 0.0,
                        "selected_orientation_correction_axis": "NONE",
                        "selected_orientation_correction_deg": 0,
                        "orientation_correction_count": 0,
                            "orientation_correction_translation_norm_m": 0.0,
                            "application_origin": (
                                "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
                            ),
                            "application_order": (
                                "parent_from_mesh @ "
                                "mesh_hypothesis_correction @ "
                                "mesh_from_semantic"
                            ),
                            "mesh_hypothesis_correction_translation_norm_m": 0.0,
                            "mesh_center_translation_preserved": True,
                            "semantic_root_translation_adjustment_norm_m": 0.0,
                            "world_up_available": True,
                            "raw_base_z_dot_world_up": 1.0,
                        "corrected_base_z_dot_world_up": 1.0,
                        "consistency_passed": True,
                        "warning": None,
                    },
                }
            ],
            frame=frame,
            alignment_id="alignment",
            skill_id="skill",
            vlm={"test": True},
            arm_is_home=False,
            keeper=FakeKeeper(),
            vio_from_camera=vio_from_camera,
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
    assert [
        measurement["source_type"]
        for measurement in result["gripper_measurements"]
    ] == ["FOUNDATIONPOSE_GRIPPER_POSE"]
    assert result["gripper_measurements"][0]["semantic_point"] == (
        "GRIPPER_MODEL_ORIGIN"
    )
    assert all(
        measurement["used_in_alignment"] is False
        for measurement in result["gripper_measurements"]
    )
    assert result["diagnostics"]["gripper_samples"]["input_count"] == 4
    assert (
        result["diagnostics"]["orientation_fit"]["method"]
        == "SINGLE_DISCRETE_BASE_ORIENTATION_SELECTION"
    )
    assert result["diagnostics"]["orientation_fit"]["selected_flip_deg"] == 0
    assert result["diagnostics"]["base_translation_authority"] == (
        "FOUNDATIONPOSE_CENTERED_MESH_THEN_MESH_FROM_SEMANTIC_ROOT"
    )
    assert np.allclose(
        result["world_from_base"]["translation_m"],
        [1.0, 0.0, 0.0],
    )
    assert (
        result["diagnostics"]["stationary_camera_vio_drift"][
            "used_in_alignment"
        ]
        is False
    )
    assert (
        result["diagnostics"]["stationary_camera_vio_drift"][
            "translation_delta_norm_m"
        ]
        > 1.0
    )


def test_semantic_quality_reports_axis_review_and_warning_success() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    diagnostics = {
        "foundation_pose_validation": [
            {
                "accepted": True,
                "acceptance_mode": "BEST_OF_TWO_SIZE_WARNING",
                "scale_review": {
                    "within_tolerance": False,
                    "equivalent_linear_scale_ratio": 1.27,
                    "mismatch_fraction": 0.27,
                    "maximum_mismatch_fraction": 0.25,
                },
                "axis_review": {
                    "base_x_relation_to_gripper": "UNCLEAR",
                    "notes": "The arrow is partly occluded.",
                },
                "base_up_alignment": {
                    "status": "TILT_WARNING",
                    "world_up_available": True,
                    "base_z_dot_world_up": 0.91,
                    "base_z_tilt_from_world_up_deg": 18.19,
                    "warning_tilt_deg": 10.0,
                    "warning": "Base +Z differs from gravity up.",
                    "transform_modified": False,
                },
                "warnings": [
                    "Base +Z differs from gravity up.",
                    "Both size attempts exceeded 25 percent.",
                ],
            }
        ],
        "orientation_fit": {
            "consistency_passed": True,
            "base_x_relation_to_gripper": "UNCLEAR",
            "selected_flip_deg": 0,
            "yaw_correction_translation_norm_m": 0.0,
            "fitted_yaw_deg": 0.0,
            "world_up_available": True,
            "raw_base_z_dot_world_up": 0.95,
            "corrected_base_z_dot_world_up": 0.95,
            "selected_orientation_correction_axis": "NONE",
            "selected_orientation_correction_deg": 0,
            "orientation_correction_count": 0,
            "orientation_correction_translation_norm_m": 0.0,
            "application_origin": (
                "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
            ),
            "application_order": (
                "parent_from_mesh @ mesh_hypothesis_correction @ "
                "mesh_from_semantic"
            ),
            "mesh_hypothesis_correction_translation_norm_m": 0.0,
            "mesh_center_translation_preserved": True,
            "semantic_root_translation_adjustment_norm_m": 0.0,
            "warning": (
                "The VLM could not determine the base +X relation."
            ),
        },
    }

    quality = skill._semantic_alignment_quality(diagnostics)

    assert quality is not None
    assert quality["status"] == "PASSED_WITH_WARNINGS"
    assert quality["base_x_relation_to_gripper"] == "UNCLEAR"
    assert quality["selected_base_yaw_flip_deg"] == 0
    assert quality["fitted_base_yaw_deg"] == 0.0
    assert quality["yaw_correction_translation_norm_m"] == 0.0
    assert quality["corrected_base_z_dot_world_up"] == 0.91
    assert (
        quality["orientation_resolution_corrected_base_z_dot_world_up"]
        == 0.95
    )
    assert quality["projected_visual_linear_scale_ratio"] == 1.27
    assert quality["base_up_status"] == "TILT_WARNING"
    assert quality["warnings"]


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
        observations={
            "vio_status": {
                "provider_id": "localization.local_vio",
                "provider_instance_id": "vio-instance",
                "boot_id": "vio-boot",
                "observed_at_us": 100,
            },
        },
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
    assert candidate["vio_provenance"] == {
        "provider_id": "localization.local_vio",
        "provider_instance_id": "vio-instance",
        "boot_id": "vio-boot",
        "world_frame": "vio",
        "session_epoch": "epoch",
        "reference_timestamp_us": 100,
    }


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
    camera_system_beak = np.asarray(
        [0.1, 0.2, 0.3],
        dtype=np.float64,
    )
    vlm = {
        "gripper": {"confidence": 0.95},
    }

    with tempfile.TemporaryDirectory() as temporary:
        result = asyncio.run(
            skill._vlm_gripper_only(
                prior=prior,
                frame=frame,
                camera_system_beak=camera_system_beak,
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
        camera_system_beak,
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


def test_vio_epoch_bridge_accepts_same_canonical_camera_after_restart() -> None:
    prior = {
        "candidate": {
            "camera_provenance": {
                "provider_id": "camera.fixed",
                "provider_instance_id": "old-instance",
                "boot_id": "old-boot",
                "canonical_device_id": "camera:serial-1",
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
                "provider_instance_id": "new-instance",
                "boot_id": "new-boot",
            },
            "device_info": {
                "data": {"canonical_device_id": "camera:serial-1"},
            },
        },
    )

    assert AlignmentSkill._same_stationary_camera_identity(prior, frame)
    frame.observations["device_info"]["data"]["canonical_device_id"] = (
        "camera:serial-2"
    )
    assert not AlignmentSkill._same_stationary_camera_identity(prior, frame)


def test_posthoc_tool_beak_learning_uses_base_and_tool_transforms() -> None:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    vio_from_base = translated(1.0, 2.0, 3.0)
    base_from_tool = translated(0.2, 0.1, 0.3)
    expected_tool_from_beak = np.asarray([0.04, -0.02, 0.05])
    vio_beak = (
        vio_from_base
        @ base_from_tool
        @ np.asarray([*expected_tool_from_beak, 1.0])
    )[:3]

    learned, diagnostics = skill._bounded_tool_beak_estimate(
        oriented=vio_from_base,
        base_from_tool=base_from_tool,
        vio_beak=vio_beak,
    )

    assert learned is not None
    assert np.allclose(learned, expected_tool_from_beak)
    assert diagnostics["accepted_for_later_refinement"] is True
