from __future__ import annotations

import copy
import asyncio
from pathlib import Path

import numpy as np
import pytest

from refine_arm_root_translation import ManagerCompactAlignmentStore


SKILL_ROOT = Path(__file__).resolve().parents[2]


def generic_effector_profile() -> dict:
    return {
        "schema": "midbrain.effector_alignment_profile",
        "schema_version": 1,
        "profile_id": "example_parallel_gripper",
        "profile_revision": "example-parallel-gripper-v1",
        "display_name": "Example Parallel Gripper",
        "assembly_type": "REPLACEABLE_EFFECTOR",
        "qualification_state": "DEVELOPMENT",
        "robot_compatibility": {
            "model_id": "example_six_axis_arm",
            "model_revision": "example-six-axis-arm-v3",
            "arm_base_frame": "example_arm_base",
            "terminal_frame": "example_arm_flange",
            "controlled_frame": "example_gripper_tcp",
        },
        "kinematic_attachment": {
            "source_schema": "physical_agent.robot_arm_model",
            "source_reference": "arm-model-v3#/fixed_tool",
            "parent_link": "wrist_link",
            "controlled_link": "example_gripper_tcp",
            "terminal_joint_to_controlled_frame": {
                "translation_m": [0.0, 0.0, 0.12],
                "rpy_rad": [0.0, 0.0, 0.0],
            },
            "qualification": "EXAMPLE_QUALIFIED",
            "replacement_policy": (
                "Every replacement effector supplies an independent profile."
            ),
        },
        "capture_motion_policy": {
            "maximum_landmark_motion_m": 0.005,
            "additional_camera_timing_margin_us": 20_000,
            "arm_feedback_age_field_path": ["data", "feedback_age_ms"],
            "fallback_arm_feedback_age_ms": 20.0,
            "maximum_arm_feedback_age_ms": 100.0,
            "preferred_arm_feedback_observation_age_ms": 100.0,
            "maximum_transform_wait_ms": 350.0,
            "transform_retry_interval_ms": 10.0,
            "temporal_sample_count": 5,
            "qualification": "EXAMPLE_DEVELOPMENT_DEFAULTS",
            "policy_note": (
                "Example timing policy for one timestamped arm provider."
            ),
        },
        "refinement_policy": {
            "second_vlm_review_raw_delta_threshold_m": 0.004,
            "maximum_raw_translation_delta_m": 0.08,
            "maximum_adopted_translation_delta_m": 0.02,
            "minimum_landmark_confidence": 0.8,
            "minimum_same_surface_confidence": 0.8,
        },
        "default_visual_alignment_landmark": "housing_center",
        "landmark_fallback_policy": {
            "selection_order": ["housing_center"],
            "automatic_substitution_allowed": False,
            "no_hardware_modification_strategy": (
                "Use the qualified rigid gripper housing center."
            ),
            "optional_physical_fiducial_policy": (
                "Optional fiducials require another profile revision."
            ),
        },
        "visual_alignment_landmarks": [
            {
                "landmark_id": "housing_center",
                "display_name": "Rigid housing center",
                "geometry": "SINGLE_REGISTERED_3D_POINT",
                "required_point_ids": ["housing_center"],
                "aggregation_policy": {
                    "method": "ARITHMETIC_MEAN_OF_ALL_REGISTERED_3D_POINTS",
                    "requires_all_points": True,
                    "missing_point_policy": "REJECT_OBSERVATION",
                },
                "description_for_vlm": (
                    "Locate the center of the rigid matte gripper housing face."
                ),
                "tool_point_binding": {
                    "source": "PROFILE_MEASUREMENT",
                    "runtime_key": None,
                    "translation_m": [0.0, 0.0, -0.03],
                    "qualification": "EXAMPLE_QUALIFIED",
                },
                "visibility": {
                    "material_note": "Matte housing",
                    "missing_exact_depth_policy": "REJECT",
                },
            }
        ],
        "action_frames": [
            {
                "frame_id": "example_gripper_tcp",
                "semantic_role": "IK_CONTROLLED_FRAME",
            }
        ],
        "invalidation_conditions": ["Effector replacement"],
    }


def active_record() -> dict:
    return {
        "activation_id": "activation-1",
        "state": "ACTIVE",
        "enforcement": "ENFORCED",
        "motion_usable": True,
        "translation_refinement_revision": 0,
        "world_frame": "workcell/example",
        "arm_base_frame": "example_arm_base",
        "session_epoch": "world-epoch-1",
        "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
        "camera_provider_id": "camera.example",
        "camera_provider_instance_id": "camera-instance-1",
        "camera_boot_id": "camera-boot-1",
        "camera_calibration_revision": "camera-cal-1",
        "transforms": {
            "world_from_base": {
                "translation_m": [0.1, 0.2, 0.3],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        },
        "last_translation_refinement": None,
    }


class FakeManager:
    def __init__(self) -> None:
        self.record = active_record()
        self.requests: list[dict] = []

    async def workcell_calibrations(self):
        return {"activations": [copy.deepcopy(self.record)]}

    async def refine_workcell_calibration_translation(self, request: dict):
        self.requests.append(copy.deepcopy(request))
        assert request["expected_refinement_revision"] == (
            self.record["translation_refinement_revision"]
        )
        self.record["translation_refinement_revision"] += 1
        self.record["transforms"]["world_from_base"] = copy.deepcopy(
            request["proposed_world_from_base"]
        )
        return copy.deepcopy(self.record)


async def generic_arm_identity(_record: dict) -> dict:
    return {
        "arm_provider_id": "robot_arm.example",
        "arm_provider_instance_id": "arm-instance-1",
        "arm_boot_id": "arm-boot-1",
        "arm_model_id": "example_six_axis_arm",
        "arm_model_revision": "example-six-axis-arm-v3",
        "assembly_id": "example-assembly",
        "assembly_revision": "example-assembly-v1",
        "assembly_fingerprint": "example-assembly-fingerprint",
        "effector_profile_id": "example_parallel_gripper",
        "effector_profile_revision": "example-parallel-gripper-v1",
        "effector_profile_sha256": None,
    }


def test_generic_arm_profile_drives_manager_snapshot_and_cas() -> None:
    manager = FakeManager()
    store = ManagerCompactAlignmentStore(
        manager,
        profile=generic_effector_profile(),
        arm_identity_source=generic_arm_identity,
    )

    snapshot = asyncio.run(store.snapshot())
    proposed = copy.deepcopy(snapshot)
    proposed["revision"] = 1
    proposed["world_from_base"][0][3] += 0.004
    refinement = {
        "schema": "midbrain.arm_root_translation_refinement",
        "schema_version": 1,
        "status": "TRANSLATION_UPDATE_READY",
    }

    applied = asyncio.run(
        store.compare_and_swap(
            expected_revision=0,
            state=proposed,
            refinement=refinement,
        )
    )

    assert applied
    requested_pose = manager.requests[0]["proposed_world_from_base"]
    assert np.allclose(requested_pose["translation_m"], [0.104, 0.2, 0.3])
    assert requested_pose["rotation_xyzw"] == [0.0, 0.0, 0.0, 1.0]
    assert manager.requests[0]["refinement"] == refinement
    assert snapshot["identities"]["arm_model_id"] == (
        "example_six_axis_arm"
    )


def test_manager_store_rejects_profile_arm_revision_mismatch() -> None:
    async def wrong_revision(_record: dict) -> dict:
        identity = await generic_arm_identity(_record)
        identity["arm_model_revision"] = "different-revision"
        return identity

    store = ManagerCompactAlignmentStore(
        FakeManager(),
        profile=generic_effector_profile(),
        arm_identity_source=wrong_revision,
    )

    with pytest.raises(RuntimeError, match="does not match the effector profile"):
        asyncio.run(store.snapshot())


def test_manager_store_treats_process_rpc_409_as_compare_and_swap_conflict() -> None:
    class ConflictError(RuntimeError):
        status_code = 409

    class ConflictManager(FakeManager):
        async def refine_workcell_calibration_translation(self, request: dict):
            raise ConflictError("409 Conflict")

    store = ManagerCompactAlignmentStore(
        ConflictManager(),
        profile=generic_effector_profile(),
        arm_identity_source=generic_arm_identity,
    )
    snapshot = asyncio.run(store.snapshot())
    proposed = copy.deepcopy(snapshot)
    proposed["world_from_base"][0][3] += 0.004

    applied = asyncio.run(
        store.compare_and_swap(
            expected_revision=0,
            state=proposed,
            refinement={
                "schema": "midbrain.arm_root_translation_refinement",
                "schema_version": 1,
                "status": "TRANSLATION_UPDATE_READY",
            },
        )
    )

    assert not applied


def test_manager_store_rejects_profile_arm_base_frame_mismatch() -> None:
    manager = FakeManager()
    manager.record["arm_base_frame"] = "different_arm_base"
    store = ManagerCompactAlignmentStore(
        manager,
        profile=generic_effector_profile(),
        arm_identity_source=generic_arm_identity,
    )

    with pytest.raises(RuntimeError, match="arm-base frame"):
        asyncio.run(store.snapshot())


def test_production_python_contains_no_arm_specific_identifier() -> None:
    production = SKILL_ROOT / "python" / "refine_arm_root_translation"
    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in production.glob("*.py")
    )

    assert "rebot" not in text
    assert "b601" not in text
