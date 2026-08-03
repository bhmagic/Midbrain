from __future__ import annotations

import copy
import math
import unittest

import httpx

from physical_agent_test.integrated_motion_adapter import (
    IntegratedRelativeMotionAdapter,
)
from physical_agent_test.spatial_frames import (
    SpatialFrameResolver,
    WORLD_CONVENTION_ID,
)


class _SpatialFabric:
    def __init__(self):
        self.transform_error: Exception | None = None
        self.rotation_xyzw = [0.0, 0.0, 0.0, 1.0]
        self.tracking_state = "TRACKING"
        self.latest_calls = 0
        self.transform_calls = 0

    async def latest_optional(self, stream):
        assert stream == "localization.vio.status"
        self.latest_calls += 1
        return {
            "observed_at_us": 1_000_000,
            "data": {
                "tracking_state": self.tracking_state,
                "world_frame": "local_vio/epoch-1",
                "session_epoch": "epoch-1",
                "camera_level_frame": "camera_level/front/epoch-1",
                "convention_id": WORLD_CONVENTION_ID,
            },
        }

    async def transform(self, **arguments):
        self.transform_calls += 1
        if self.transform_error is not None:
            raise self.transform_error
        return {
            "from_frame": arguments["from_frame"],
            "to_frame": arguments["to_frame"],
            "at_us": arguments["at_us"],
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": list(self.rotation_xyzw),
            "path": [{"authority": "test"}],
        }


def _adapter(
    client,
    fabric: _SpatialFabric | None = None,
    *,
    vio_readiness_checker=None,
    visual_evidence_capture=None,
    require_visual_verification: bool = False,
    attempt_visual_verification: bool = False,
    require_upright_mount_confirmation: bool = False,
    calibration_activation_continuation=None,
):
    spatial_fabric = fabric or _SpatialFabric()
    return IntegratedRelativeMotionAdapter(
        client,
        SpatialFrameResolver(
            spatial_fabric,
            arm_base_frame="rebot_arm_base",
        ),
        vio_readiness_checker=vio_readiness_checker,
        visual_evidence_capture=visual_evidence_capture,
        require_visual_verification=require_visual_verification,
        attempt_visual_verification=attempt_visual_verification,
        require_upright_mount_confirmation=(
            require_upright_mount_confirmation
        ),
        calibration_activation_continuation=(
            calibration_activation_continuation
        ),
    )


class _ReadinessChecker:
    def __init__(self, fabric: _SpatialFabric):
        self.fabric = fabric
        self.calls = 0

    async def __call__(self, _reason):
        self.calls += 1
        self.fabric.tracking_state = "TRACKING"
        return {
            "status": "tracking_ready",
            "result": {
                "world_frame": "local_vio/epoch-1",
                "session_epoch": "epoch-1",
                "epoch_reset_performed": False,
            },
        }


class _VisualEvidence:
    def __init__(self):
        self.points = [
            [0.1, 0.2, 0.3],
            [0.1, 0.2, 0.5],
        ]
        self.calls: list[str] = []

    async def __call__(self, world_frame):
        self.calls.append(world_frame)
        point = self.points[len(self.calls) - 1]
        return {
            "target_frame": world_frame,
            "camera_capture": {
                "evidence_id": f"picture-{len(self.calls)}",
            },
            "control_reference": {
                "target_point_m": list(point),
            },
        }


class _UnavailableDepthEvidence:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, world_frame):
        self.calls.append(world_frame)
        raise RuntimeError(
            "effector-front point front_1 has no valid exact depth"
        )


class _IntegratedClient:
    def __init__(self):
        self.engage_count = 0
        self.trigger_count = 0
        self.completion_success = True
        self.last_preview_request = None
        self.reject_preview = False
        self.rejected_preview_plan = None
        self.preview_duration_override_s = None
        self.snapshot = {
            "residency": "HOT",
            "ready": True,
            "health": "HEALTHY",
            "controller_identity": {
                "provider_id": "robot_arm.primary.integrated",
                "provider_instance_id": "controller-instance",
                "boot_id": "controller-boot",
                "configuration_sha256": "configuration-sha",
            },
            "commit_count": 0,
            "model_view": {
                "measured_controlled_frame": {
                    "position_m": [0.1, 0.2, 0.3],
                    "rpy_rad": [0.2, -0.1, 0.4],
                },
                "staged_controlled_frame": {
                    "position_m": [0.1, 0.2, 0.3],
                    "rpy_rad": [0.2, -0.1, 0.4],
                },
            },
            "planning": {
                "target_revision": 1,
                "last_preview": None,
            },
            "trajectory": {
                "active": False,
                "last_completed": None,
            },
        }

    async def state(self):
        return copy.deepcopy(self.snapshot)

    async def preview_direct_motion(self, request):
        self.last_preview_request = copy.deepcopy(request)
        requested_duration_s = float(
            request["command"]["settings"]["duration_s"]
        )
        planned_duration_s = (
            float(self.preview_duration_override_s)
            if self.preview_duration_override_s is not None
            else requested_duration_s
        )
        target = request["command"]["target"]["position_m"]
        target_rpy = request["command"]["target"].get("rpy_rad")
        self.snapshot["model_view"]["staged_controlled_frame"] = {
            "position_m": list(target),
            "rpy_rad": (
                list(target_rpy)
                if target_rpy is not None
                else list(
                    self.snapshot["model_view"][
                        "measured_controlled_frame"
                    ]["rpy_rad"]
                )
            ),
        }
        if self.reject_preview:
            return {
                "status": "REJECTED",
                "preview": copy.deepcopy(self.rejected_preview_plan) if (
                    self.rejected_preview_plan is not None
                ) else {
                    "planning_valid": False,
                    "planning_reasons": [
                        "IK endpoint requires excessive joint travel on joints 3"
                    ],
                    "endpoint_joint_delta_rad": [
                        0.0,
                        0.49,
                        0.818,
                        0.33,
                        0.0,
                        0.0,
                    ],
                    "endpoint_joint_delta_limit_rad": [
                        0.8,
                        0.8,
                        0.8,
                        1.0,
                        1.0,
                        1.0,
                    ],
                },
            }
        self.snapshot["planning"] = {
            "target_revision": 2,
            "last_preview": {
                "preview_id": "preview-1",
                "planning_valid": True,
                "target_revision": 2,
                "duration_s": planned_duration_s,
            },
        }
        return {
            "status": "PLANNED",
            "plan_id": "preview-1",
            "preview": {
                "preview_id": "preview-1",
                "planning_valid": True,
                "duration_s": planned_duration_s,
            },
        }

    async def engage_staged_motion(self):
        self.engage_count += 1
        return {"status": "engaged_target_edit", "engaged": True}

    async def trigger_one_shot_motion(self):
        self.trigger_count += 1
        self.snapshot["commit_count"] += 1
        target = self.snapshot["model_view"][
            "staged_controlled_frame"
        ]["position_m"]
        self.snapshot["model_view"]["measured_controlled_frame"] = {
            "position_m": (
                list(target)
                if self.completion_success
                else [target[0], target[1], target[2] - 0.006]
            ),
            "rpy_rad": list(
                self.snapshot["model_view"][
                    "staged_controlled_frame"
                ]["rpy_rad"]
            ),
        }
        self.snapshot["trajectory"] = {
            "active": False,
            "last_completed": {
                "completion_success": self.completion_success,
                "completion_outcome": (
                    "ARRIVAL_CONFIRMED_AND_FLOATED"
                    if self.completion_success
                    else "DEADLINE_FLOAT_BEFORE_ARRIVAL"
                ),
                "duration_s": self.snapshot["planning"]["last_preview"][
                    "duration_s"
                ],
            },
        }
        return {
            "accepted": True,
            "physical_motion_authorized": True,
        }


class _OfflineIntegratedClient:
    async def state(self):
        raise httpx.ConnectError("All connection attempts failed")


class IntegratedRelativeMotionAdapterTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_up_preview_uses_world_z_then_resolves_to_arm_base(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)

        result = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(result["status"], "PREVIEW_READY")
        self.assertFalse(result["workflow_complete"])
        self.assertEqual(result["target_position_m"], [0.1, 0.2, 0.5])
        self.assertEqual(result["reference_frame"], "WORLD")
        self.assertEqual(
            result["resolved_direction_arm_base"],
            [0.0, 0.0, 1.0],
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "execute_integrated_motion_preview",
                "arguments": {
                    "preview_id": "preview-1",
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "UP",
                    "reference_frame": "WORLD",
                    "resolved_direction_arm_base": [0.0, 0.0, 1.0],
                    "distance_m": 0.2,
                    "original_request_distance_m": 0.2,
                    "requested_speed_m_s": None,
                    "requested_duration_s": 3.0,
                    "planned_duration_s": 3.0,
                    "planned_nominal_speed_m_s": 0.2 / 3.0,
                    "timing_safety_limited": False,
                    "target_position_m": [0.1, 0.2, 0.5],
                    "orientation_policy": "POSITION_ONLY",
                    "controlled_frame_yaw_delta_deg": None,
                    "target_orientation_rpy_rad": None,
                },
            },
        )
        self.assertEqual(client.engage_count, 0)

    async def test_explicit_speed_derives_and_binds_trajectory_duration(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)

        preview = await adapter.preview(
            direction="NEGATIVE_X",
            distance_m=0.1,
            requested_speed_m_s=0.2,
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["motion_intent"],
            "NEW_RELATIVE_MOVE",
        )
        self.assertEqual(
            client.last_preview_request["command"]["settings"]["duration_s"],
            0.5,
        )
        self.assertEqual(preview["requested_speed_m_s"], 0.2)
        self.assertEqual(preview["requested_duration_s"], 0.5)
        self.assertEqual(preview["planned_duration_s"], 0.5)
        self.assertEqual(preview["planned_nominal_speed_m_s"], 0.2)
        self.assertFalse(preview["timing_safety_limited"])

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(result["controller_duration_s"], 0.5)
        self.assertFalse(result["timing"]["constant_cartesian_speed"])

    async def test_provider_may_lengthen_requested_speed_for_safety(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.preview_duration_override_s = 1.2
        adapter = _adapter(client)

        preview = await adapter.preview(
            direction="NEGATIVE_X",
            distance_m=0.1,
            requested_speed_m_s=0.2,
        )

        self.assertEqual(preview["requested_duration_s"], 0.5)
        self.assertEqual(preview["planned_duration_s"], 1.2)
        self.assertAlmostEqual(
            preview["planned_nominal_speed_m_s"],
            0.1 / 1.2,
        )
        self.assertTrue(preview["timing_safety_limited"])

    async def test_invalid_or_unrepresentable_speed_is_rejected_before_stage(
        self,
    ) -> None:
        for speed in (0.0, -0.1, math.nan, 0.21, 0.001):
            with self.subTest(speed=speed):
                client = _IntegratedClient()
                result = await _adapter(client).preview(
                    direction="UP",
                    distance_m=0.1,
                    requested_speed_m_s=speed,
                )

                self.assertEqual(
                    result["status"],
                    "RELATIVE_MOTION_TIMING_UNSUPPORTED",
                )
                self.assertIsNone(client.last_preview_request)

    async def test_tampered_timing_cannot_execute_stored_preview(self) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(
            direction="UP",
            distance_m=0.1,
            requested_speed_m_s=0.2,
        )
        arguments = dict(preview["required_next_tool"]["arguments"])
        arguments["planned_duration_s"] = 0.6

        with self.assertRaisesRegex(RuntimeError, "do not match"):
            await adapter.execute(**arguments)

        self.assertEqual(client.engage_count, 0)

    async def test_preserve_head_direction_uses_measured_pose_6dof(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)

        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            orientation_policy="PRESERVE_MEASURED_CONTROLLED_FRAME",
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["motion_intent"],
            "NEW_RELATIVE_MOVE",
        )
        self.assertEqual(
            preview["orientation_policy"],
            "PRESERVE_MEASURED_CONTROLLED_FRAME",
        )
        self.assertEqual(
            preview["target_orientation_rpy_rad"],
            [0.2, -0.1, 0.4],
        )
        command = client.last_preview_request["command"]
        self.assertEqual(command["settings"]["ik_mode"], "POSE_6DOF")
        self.assertEqual(
            command["target"],
            {
                "position_m": [0.1, 0.2, 0.5],
                "rpy_rad": [0.2, -0.1, 0.4],
            },
        )

        arguments = preview["required_next_tool"]["arguments"]
        result = await adapter.execute(**arguments)

        self.assertEqual(
            result["orientation_policy"],
            "PRESERVE_MEASURED_CONTROLLED_FRAME",
        )
        self.assertEqual(
            result["target_orientation_rpy_rad"],
            [0.2, -0.1, 0.4],
        )
        self.assertTrue(result["physical_motion_completed"])

    async def test_rotation_only_turns_controlled_head_right_without_spatial_resolution(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.snapshot["model_view"]["measured_controlled_frame"][
            "rpy_rad"
        ] = [0.0, 0.0, 0.0]
        fabric = _SpatialFabric()
        fabric.tracking_state = "DEGRADED"
        evidence = _VisualEvidence()
        adapter = _adapter(
            client,
            fabric,
            visual_evidence_capture=evidence,
            require_visual_verification=True,
        )

        preview = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["motion_intent"],
            "NEW_RELATIVE_ROTATION",
        )
        self.assertEqual(preview["target_position_m"], [0.1, 0.2, 0.3])
        self.assertEqual(preview["reference_frame"], "CONTROLLED_FRAME")
        self.assertEqual(
            preview["orientation_reference_frame"],
            "CONTROLLED_FRAME",
        )
        self.assertEqual(
            preview["target_orientation_rpy_rad"],
            [0.0, 0.0, -math.pi / 6.0],
        )
        self.assertEqual(preview["planned_nominal_speed_m_s"], 0.0)
        self.assertEqual(
            preview["spatial_resolution"]["provenance"][
                "resolution_source"
            ],
            "ROTATION_ONLY_NO_TRANSLATION",
        )
        self.assertEqual(fabric.latest_calls, 0)
        self.assertEqual(fabric.transform_calls, 0)
        self.assertEqual(evidence.calls, [])
        self.assertEqual(
            preview["visual_verification"]["status"],
            "SKIPPED_ROTATION_ONLY_NO_ORIENTATION_EVIDENCE",
        )
        command = client.last_preview_request["command"]
        self.assertEqual(command["settings"]["execution_mode"], "PRESS_MIT")
        self.assertEqual(command["settings"]["ik_mode"], "POSE_6DOF")

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertTrue(result["physical_motion_completed"])
        self.assertEqual(
            result["controlled_frame_yaw_delta_deg"],
            -30.0,
        )
        self.assertEqual(
            result["timing"]["semantics"],
            "DEFAULT_DURATION_WITH_JOINT_RATE_SAFETY",
        )

    async def test_combined_arm_forward_and_intrinsic_head_yaw_uses_one_pose_target(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)

        preview = await adapter.preview(
            direction="ARM_BASE_POSITIVE_X",
            reference_frame="ARM_BASE",
            distance_m=0.2,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["motion_intent"],
            "NEW_RELATIVE_POSE_MOVE",
        )
        for actual, expected in zip(
            preview["target_position_m"],
            [0.3, 0.2, 0.3],
            strict=True,
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(
            preview["controlled_frame_yaw_delta_deg"],
            -30.0,
        )
        self.assertNotEqual(
            preview["target_orientation_rpy_rad"],
            [0.2, -0.1, 0.4 - math.pi / 6.0],
        )
        command = client.last_preview_request["command"]
        self.assertEqual(command["settings"]["ik_mode"], "POSE_6DOF")
        self.assertEqual(
            command["target"]["rpy_rad"],
            preview["target_orientation_rpy_rad"],
        )

    async def test_combined_pose_visual_evidence_confirms_translation_not_yaw(
        self,
    ) -> None:
        client = _IntegratedClient()
        fabric = _SpatialFabric()
        evidence = _VisualEvidence()
        adapter = _adapter(
            client,
            fabric,
            vio_readiness_checker=_ReadinessChecker(fabric),
            visual_evidence_capture=evidence,
            attempt_visual_verification=True,
        )
        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            fixed_vio_rig_assumption="CONFIRMED_FIXED_STATIONARY_RIG",
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(
            result["status"],
            "MOTION_COMPLETED_TRANSLATION_VISUALLY_CONFIRMED_"
            "ORIENTATION_UNVERIFIED",
        )
        self.assertTrue(result["visual_translation_confirmed"])
        self.assertFalse(result["visual_orientation_confirmed"])
        self.assertFalse(result["visual_motion_confirmed"])
        self.assertIn("did not measure or confirm", result["message"])

    async def test_rotation_only_input_invariants_and_yaw_bound_fail_closed(
        self,
    ) -> None:
        cases = [
            {
                "direction": "UP",
                "distance_m": 0.0,
                "orientation_policy": "APPLY_CONTROLLED_FRAME_YAW_DELTA",
                "controlled_frame_yaw_delta_deg": -30.0,
            },
            {
                "direction": "NONE",
                "distance_m": 0.1,
                "orientation_policy": "APPLY_CONTROLLED_FRAME_YAW_DELTA",
                "controlled_frame_yaw_delta_deg": -30.0,
            },
            {
                "direction": "NONE",
                "distance_m": 0.0,
                "orientation_policy": "PRESERVE_MEASURED_CONTROLLED_FRAME",
            },
            {
                "direction": "NONE",
                "distance_m": 0.0,
                "orientation_policy": "APPLY_CONTROLLED_FRAME_YAW_DELTA",
                "controlled_frame_yaw_delta_deg": 45.001,
            },
            {
                "direction": "NONE",
                "distance_m": 0.0,
                "requested_speed_m_s": 0.1,
                "orientation_policy": "APPLY_CONTROLLED_FRAME_YAW_DELTA",
                "controlled_frame_yaw_delta_deg": -30.0,
            },
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                client = _IntegratedClient()
                with self.assertRaises(ValueError):
                    await _adapter(client).preview(**arguments)
                self.assertIsNone(client.last_preview_request)

    async def test_tampered_controlled_frame_yaw_cannot_execute_preview(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )
        arguments = dict(preview["required_next_tool"]["arguments"])
        arguments["controlled_frame_yaw_delta_deg"] = -29.0

        with self.assertRaisesRegex(RuntimeError, "do not match"):
            await adapter.execute(**arguments)

        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_orientation_change_invalidates_yaw_delta_preview(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )
        client.snapshot["model_view"]["measured_controlled_frame"][
            "rpy_rad"
        ][2] += 0.03

        with self.assertRaisesRegex(RuntimeError, "orientation changed"):
            await adapter.execute(
                **preview["required_next_tool"]["arguments"]
            )

        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_rejected_ik_preview_returns_axis_and_joint_diagnostics(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            orientation_policy="PRESERVE_MEASURED_CONTROLLED_FRAME",
        )

        self.assertEqual(result["status"], "IK_PREVIEW_REJECTED")
        self.assertEqual(
            result["resolved_direction_arm_base"],
            [0.0, 0.0, 1.0],
        )
        self.assertEqual(result["start_position_m"], [0.1, 0.2, 0.3])
        self.assertEqual(result["target_position_m"], [0.1, 0.2, 0.5])
        self.assertEqual(
            result["controller_preview"]["endpoint_joint_delta_rad"][2],
            0.818,
        )
        self.assertEqual(
            result["controller_preview"][
                "endpoint_joint_delta_limit_rad"
            ][2],
            0.8,
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(client.engage_count, 0)

    async def test_collision_free_endpoint_travel_rejection_is_policy_limited(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        client.rejected_preview_plan = {
            "planning_valid": False,
            "planning_reasons": [
                "IK endpoint requires excessive joint travel on joints 5"
            ],
            "collision_free": True,
            "target_clamped": False,
            "physical_motion_authorized": False,
            "physical_execution_blockers": [
                "IK endpoint requires excessive joint travel on joints 5"
            ],
            "position_residual_m": 0.000336895,
            "orientation_residual_rad": 0.02182257,
            "endpoint_joint_delta_rad": [
                0.722666,
                0.264718,
                0.000646,
                0.304533,
                1.225,
                0.052603,
            ],
            "endpoint_joint_delta_limit_rad": [
                0.8,
                0.8,
                0.85,
                1.0,
                1.0,
                1.0,
            ],
            "cartesian_continuity": {
                "total_joint_travel_rad": 2.58595,
            },
        }
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(
            result["status"],
            "REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED",
        )
        self.assertEqual(result["classification"], result["status"])
        self.assertFalse(result["physical_motion_authorized"])
        self.assertNotIn("required_next_tool", result)
        diagnostics = result["policy_limit_diagnostics"]
        self.assertEqual(
            diagnostics["guard_codes"],
            ["ENDPOINT_JOINT_TRAVEL"],
        )
        violations = diagnostics["endpoint_joint_violations"]
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["joint"], 5)
        self.assertEqual(violations[0]["delta_rad"], 1.225)
        self.assertEqual(violations[0]["limit_rad"], 1.0)
        self.assertAlmostEqual(
            violations[0]["excess_rad"],
            0.225,
        )
        self.assertFalse(
            diagnostics["automatic_segmentation_performed"]
        )
        self.assertIn("will not automatically segment", result["message"])
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_high_residual_travel_rejection_remains_generic(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        client.rejected_preview_plan = {
            "planning_valid": False,
            "planning_reasons": [
                "IK endpoint requires excessive joint travel on joints 5"
            ],
            "collision_free": True,
            "target_clamped": False,
            "physical_motion_authorized": False,
            "position_residual_m": 0.03,
            "orientation_residual_rad": 0.02,
            "endpoint_joint_delta_rad": [0.0, 0.0, 0.0, 0.0, 1.2, 0.0],
            "endpoint_joint_delta_limit_rad": [
                0.8,
                0.8,
                0.85,
                1.0,
                1.0,
                1.0,
            ],
        }
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(result["status"], "IK_PREVIEW_REJECTED")
        self.assertNotIn("classification", result)
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_endpoint_and_aggregate_travel_rejection_is_policy_limited(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        client.rejected_preview_plan = {
            "planning_valid": False,
            "planning_reasons": [
                "IK endpoint requires excessive joint travel on joints 5",
                "IK path has excessive aggregate joint travel",
            ],
            "collision_free": True,
            "target_clamped": False,
            "physical_motion_authorized": False,
            "physical_execution_blockers": [
                "IK endpoint requires excessive joint travel on joints 5",
                "IK path has excessive aggregate joint travel",
            ],
            "position_residual_m": 0.0004,
            "orientation_residual_rad": 0.022,
            "endpoint_joint_delta_rad": [
                0.75,
                0.3,
                0.2,
                0.4,
                1.242913,
                0.1,
            ],
            "endpoint_joint_delta_limit_rad": [
                0.8,
                0.8,
                0.85,
                1.0,
                1.0,
                1.0,
            ],
            "cartesian_continuity": {
                "total_joint_travel_rad": 3.354,
            },
        }
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="UP",
            distance_m=0.15,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(
            result["status"],
            "REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED",
        )
        diagnostics = result["policy_limit_diagnostics"]
        self.assertEqual(
            diagnostics["guard_codes"],
            ["ENDPOINT_JOINT_TRAVEL", "AGGREGATE_JOINT_TRAVEL"],
        )
        self.assertEqual(
            diagnostics["aggregate_joint_travel"],
            {"total_rad": 3.354, "limit_rad": None, "excess_rad": None},
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_mixed_ik_and_travel_reasons_remain_generic_rejection(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        client.rejected_preview_plan = {
            "planning_valid": False,
            "planning_reasons": [
                "IK endpoint requires excessive joint travel on joints 5",
                "IK orientation residual 0.2 exceeds limit",
            ],
            "collision_free": True,
            "target_clamped": False,
            "physical_motion_authorized": False,
            "position_residual_m": 0.0004,
            "orientation_residual_rad": 0.2,
            "endpoint_joint_delta_rad": [0.0, 0.0, 0.0, 0.0, 1.2, 0.0],
            "endpoint_joint_delta_limit_rad": [
                0.8,
                0.8,
                0.85,
                1.0,
                1.0,
                1.0,
            ],
        }
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(result["status"], "IK_PREVIEW_REJECTED")
        self.assertNotIn("classification", result)
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_non_collision_free_travel_rejection_remains_generic(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.reject_preview = True
        client.rejected_preview_plan = {
            "planning_valid": False,
            "planning_reasons": [
                "IK endpoint requires excessive joint travel on joints 5"
            ],
            "collision_free": False,
            "target_clamped": False,
            "physical_motion_authorized": False,
            "position_residual_m": 0.0004,
            "orientation_residual_rad": 0.02,
            "endpoint_joint_delta_rad": [0.0, 0.0, 0.0, 0.0, 1.2, 0.0],
            "endpoint_joint_delta_limit_rad": [
                0.8,
                0.8,
                0.85,
                1.0,
                1.0,
                1.0,
            ],
        }
        adapter = _adapter(client)

        result = await adapter.preview(
            direction="NONE",
            distance_m=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )

        self.assertEqual(result["status"], "IK_PREVIEW_REJECTED")
        self.assertNotIn("classification", result)
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_orientation_change_invalidates_preserving_preview(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            orientation_policy="PRESERVE_MEASURED_CONTROLLED_FRAME",
        )
        client.snapshot["model_view"]["measured_controlled_frame"][
            "rpy_rad"
        ][2] += 0.03

        with self.assertRaisesRegex(
            RuntimeError,
            "orientation changed",
        ):
            await adapter.execute(
                **preview["required_next_tool"]["arguments"]
            )

        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_observation_reports_pending_preview_without_mutation(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        observation = await adapter.observation()

        self.assertTrue(observation["read_only"])
        self.assertEqual(
            observation["pending_previews"][0]["preview_id"],
            preview["preview_id"],
        )
        self.assertEqual(observation["controller"]["residency"], "HOT")
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_unreachable_controller_returns_recovery_route(self) -> None:
        adapter = _adapter(_OfflineIntegratedClient())

        result = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(result["retry_same_tool"])
        self.assertEqual(
            result["required_next_tool"]["name"],
            "inspect_midbrain_runtime",
        )

    async def test_recovery_required_returns_explicit_hot_transition(
        self,
    ) -> None:
        client = _IntegratedClient()
        client.snapshot["residency"] = "RECOVERY_REQUIRED"
        client.snapshot["ready"] = False
        client.snapshot["fault_reason"] = (
            "Basic lease lost: global motion inhibit"
        )
        adapter = _adapter(client)

        result = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(
            result["status"],
            "INTEGRATED_RECOVERY_REQUIRED",
        )
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": "robot_arm.primary.integrated",
                    "action": "hot",
                    "required_capability": (
                        "robot.motion.arm.integrated.mit.one_shot"
                    ),
                },
            },
        )
        self.assertIn("already running", result["message"])

    async def test_exact_preview_requires_separate_execution(self) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertTrue(result["physical_motion_completed"])
        self.assertEqual(client.engage_count, 1)
        self.assertEqual(client.trigger_count, 1)

    async def test_finished_motion_without_arrival_is_not_success(self) -> None:
        client = _IntegratedClient()
        client.completion_success = False
        adapter = _adapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(
            result["status"],
            "MOTION_FINISHED_WITHOUT_CONFIRMED_ARRIVAL",
        )
        self.assertFalse(result["physical_motion_completed"])
        self.assertIn("DEADLINE_FLOAT_BEFORE_ARRIVAL", result["message"])

        next_preview = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(next_preview["motion_intent"], "NEW_RELATIVE_MOVE")
        self.assertAlmostEqual(next_preview["start_position_m"][2], 0.494)
        self.assertAlmostEqual(next_preview["target_position_m"][2], 0.694)

    async def test_changed_preview_is_rejected(self) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)
        client.snapshot["planning"]["target_revision"] = 3

        with self.assertRaisesRegex(
            RuntimeError,
            "preview changed before approval",
        ):
            await adapter.execute(
                **preview["required_next_tool"]["arguments"]
            )

        self.assertEqual(client.engage_count, 0)

    async def test_changed_measured_start_pose_is_rejected(self) -> None:
        client = _IntegratedClient()
        adapter = _adapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)
        client.snapshot["model_view"]["measured_controlled_frame"][
            "position_m"
        ][0] += 0.01

        with self.assertRaisesRegex(
            RuntimeError,
            "measured arm pose changed",
        ):
            await adapter.execute(
                **preview["required_next_tool"]["arguments"]
            )

        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_changed_spatial_rotation_is_rejected(self) -> None:
        client = _IntegratedClient()
        fabric = _SpatialFabric()
        adapter = _adapter(client, fabric)
        preview = await adapter.preview(
            direction="FRONT",
            distance_m=0.1,
        )
        half_angle = math.pi / 4.0
        fabric.rotation_xyzw = [
            0.0,
            0.0,
            math.sin(half_angle),
            math.cos(half_angle),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "spatial resolution changed",
        ):
            await adapter.execute(
                **preview["required_next_tool"]["arguments"]
            )

        self.assertEqual(client.engage_count, 0)

    async def test_world_front_is_rotated_into_sideways_arm_base(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        half_angle = math.pi / 4.0
        fabric.rotation_xyzw = [
            0.0,
            0.0,
            math.sin(half_angle),
            math.cos(half_angle),
        ]
        adapter = _adapter(_IntegratedClient(), fabric)

        result = await adapter.preview(
            direction="FRONT",
            distance_m=0.1,
        )

        self.assertTrue(
            all(
                math.isclose(left, right, abs_tol=1e-9)
                for left, right in zip(
                    result["resolved_direction_arm_base"],
                    [0.0, -1.0, 0.0],
                    strict=True,
                )
            )
        )
        self.assertTrue(
            all(
                math.isclose(left, right, abs_tol=1e-9)
                for left, right in zip(
                    result["target_position_m"],
                    [0.1, 0.1, 0.3],
                    strict=True,
                )
            )
        )

    async def test_missing_alignment_requests_operator_attestation(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        fabric.transform_error = RuntimeError("no transform path")
        adapter = _adapter(_IntegratedClient(), fabric)

        result = await adapter.preview(direction="UP", distance_m=0.1)

        self.assertEqual(
            result["status"],
            "ARM_ALIGNMENT_OR_ATTESTATION_REQUIRED",
        )
        self.assertIn("Confirm y/n", result["question"])

        confirmed = await adapter.preview(
            direction="UP",
            distance_m=0.1,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )
        self.assertEqual(confirmed["status"], "PREVIEW_READY")
        self.assertEqual(
            confirmed["spatial_resolution"]["provenance"][
                "resolution_source"
            ],
            "OPERATOR_ATTESTED_IDENTITY_ROTATION",
        )
        self.assertEqual(
            confirmed["spatial_resolution"]["provenance"][
                "operator_attestation"
            ]["controller_identity"]["boot_id"],
            "controller-boot",
        )

    async def test_explicit_world_axis_never_uses_upright_arm_fallback(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        fabric.transform_error = RuntimeError("no transform path")
        adapter = _adapter(_IntegratedClient(), fabric)

        result = await adapter.preview(
            direction="POSITIVE_X",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )

        self.assertEqual(
            result["status"],
            "WORLD_TO_ARM_ALIGNMENT_REQUIRED",
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertIn("cannot fall back", result["message"])
        self.assertEqual(fabric.latest_calls, 1)
        self.assertEqual(fabric.transform_calls, 1)

    async def test_runtime_mount_policy_does_not_intercept_world_axis(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        fabric.transform_error = RuntimeError("no transform path")
        adapter = _adapter(
            _IntegratedClient(),
            fabric,
            require_upright_mount_confirmation=True,
        )

        result = await adapter.preview(
            direction="POSITIVE_X",
            distance_m=0.2,
            reference_frame="WORLD",
        )

        self.assertEqual(
            result["status"],
            "WORLD_TO_ARM_ALIGNMENT_REQUIRED",
        )
        self.assertNotEqual(
            result["status"],
            "ARM_MOUNT_CONFIRMATION_REQUIRED",
        )

    async def test_world_axis_uses_latest_exact_candidate_continuation(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        fabric.transform_error = RuntimeError("no transform path")
        continuation = {
            "name": "review_and_activate_stationary_calibration",
            "arguments": {
                "alignment_id": "alignment-1",
                "candidate_sha256": "a" * 64,
            },
        }
        adapter = _adapter(
            _IntegratedClient(),
            fabric,
            require_upright_mount_confirmation=True,
            calibration_activation_continuation=lambda: continuation,
        )

        result = await adapter.preview(
            direction="POSITIVE_X",
            distance_m=0.2,
            reference_frame="WORLD",
        )

        self.assertEqual(
            result["status"],
            "WORLD_TO_ARM_ALIGNMENT_REQUIRED",
        )
        self.assertEqual(result["required_next_tool"], continuation)

    async def test_reviewed_transform_precedes_upright_mount_fallback(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        half_angle = math.pi / 4.0
        fabric.rotation_xyzw = [
            0.0,
            0.0,
            math.sin(half_angle),
            math.cos(half_angle),
        ]
        adapter = _adapter(
            _IntegratedClient(),
            fabric,
            require_upright_mount_confirmation=True,
        )

        result = await adapter.preview(
            direction="FRONT",
            distance_m=0.1,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )

        self.assertEqual(result["status"], "PREVIEW_READY")
        self.assertEqual(
            result["spatial_resolution"]["provenance"][
                "resolution_source"
            ],
            "TIMESTAMPED_WORLD_FROM_ARM_TRANSFORM",
        )
        self.assertTrue(
            all(
                math.isclose(left, right, abs_tol=1e-9)
                for left, right in zip(
                    result["resolved_direction_arm_base"],
                    [0.0, -1.0, 0.0],
                    strict=True,
                )
            )
        )
        self.assertEqual(fabric.latest_calls, 1)
        self.assertEqual(fabric.transform_calls, 1)

    async def test_runtime_mount_policy_does_not_prompt_when_transform_exists(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        adapter = _adapter(
            _IntegratedClient(),
            fabric,
            require_upright_mount_confirmation=True,
        )

        result = await adapter.preview(
            direction="UP",
            distance_m=0.1,
        )

        self.assertEqual(result["status"], "PREVIEW_READY")
        self.assertEqual(
            result["spatial_resolution"]["provenance"][
                "resolution_source"
            ],
            "TIMESTAMPED_WORLD_FROM_ARM_TRANSFORM",
        )

    async def test_runtime_default_up_previews_without_vio_or_depth(
        self,
    ) -> None:
        client = _IntegratedClient()
        fabric = _SpatialFabric()
        fabric.tracking_state = "DEGRADED"
        readiness = _ReadinessChecker(fabric)
        evidence = _VisualEvidence()
        adapter = _adapter(
            client,
            fabric,
            vio_readiness_checker=readiness,
            visual_evidence_capture=evidence,
            attempt_visual_verification=True,
            require_upright_mount_confirmation=True,
        )

        confirmation = await adapter.preview(
            direction="UP",
            distance_m=0.2,
        )

        self.assertEqual(
            confirmation["status"],
            "ARM_MOUNT_CONFIRMATION_REQUIRED",
        )

        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(preview["target_position_m"], [0.1, 0.2, 0.5])
        self.assertEqual(
            preview["resolved_direction_arm_base"],
            [0.0, 0.0, 1.0],
        )
        self.assertEqual(
            preview["visual_verification"]["status"],
            "SKIPPED_FIXED_RIG_NOT_CONFIRMED",
        )
        self.assertEqual(readiness.calls, 0)
        self.assertEqual(evidence.calls, [])
        self.assertEqual(fabric.latest_calls, 2)
        self.assertEqual(fabric.transform_calls, 0)

    async def test_missing_exact_depth_does_not_block_optional_preview(
        self,
    ) -> None:
        client = _IntegratedClient()
        fabric = _SpatialFabric()
        readiness = _ReadinessChecker(fabric)
        evidence = _UnavailableDepthEvidence()
        adapter = _adapter(
            client,
            fabric,
            vio_readiness_checker=readiness,
            visual_evidence_capture=evidence,
            attempt_visual_verification=True,
            require_upright_mount_confirmation=True,
        )

        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            fixed_vio_rig_assumption=(
                "CONFIRMED_FIXED_STATIONARY_RIG"
            ),
        )

        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["visual_verification"]["status"],
            "BEFORE_EVIDENCE_UNAVAILABLE",
        )
        self.assertIn(
            "no valid exact depth",
            preview["visual_verification"]["detail"]["error"],
        )
        self.assertEqual(readiness.calls, 1)
        self.assertEqual(evidence.calls, ["local_vio/epoch-1"])

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(
            result["status"],
            "MOTION_COMPLETED_VISUAL_CHECK_UNAVAILABLE",
        )
        self.assertTrue(result["physical_motion_completed"])
        self.assertFalse(result["visual_motion_confirmed"])
        self.assertEqual(len(evidence.calls), 1)

    async def test_degraded_vio_first_requests_arm_mount_confirmation(
        self,
    ) -> None:
        fabric = _SpatialFabric()
        fabric.tracking_state = "DEGRADED"
        adapter = _adapter(_IntegratedClient(), fabric)

        result = await adapter.preview(direction="UP", distance_m=0.1)

        self.assertEqual(
            result["status"],
            "ARM_MOUNT_CONFIRMATION_REQUIRED",
        )
        self.assertIn("horizontal plane", result["question"])

        confirmed = await adapter.preview(
            direction="UP",
            distance_m=0.1,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )
        self.assertEqual(confirmed["status"], "PREVIEW_READY")

    async def test_visual_workflow_orders_confirmations_and_uses_two_pictures(
        self,
    ) -> None:
        client = _IntegratedClient()
        fabric = _SpatialFabric()
        fabric.tracking_state = "DEGRADED"
        readiness = _ReadinessChecker(fabric)
        evidence = _VisualEvidence()
        adapter = _adapter(
            client,
            fabric,
            vio_readiness_checker=readiness,
            visual_evidence_capture=evidence,
            require_visual_verification=True,
        )

        arm_question = await adapter.preview(
            direction="UP",
            distance_m=0.2,
        )
        self.assertEqual(
            arm_question["status"],
            "ARM_MOUNT_CONFIRMATION_REQUIRED",
        )
        self.assertEqual(readiness.calls, 0)

        rig_question = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
        )
        self.assertEqual(
            rig_question["status"],
            "FIXED_VIO_RIG_CONFIRMATION_REQUIRED",
        )
        self.assertEqual(readiness.calls, 0)

        preview = await adapter.preview(
            direction="UP",
            distance_m=0.2,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            fixed_vio_rig_assumption=(
                "CONFIRMED_FIXED_STATIONARY_RIG"
            ),
        )
        self.assertEqual(preview["status"], "PREVIEW_READY")
        self.assertEqual(
            preview["visual_verification"]["status"],
            "BEFORE_EVIDENCE_READY",
        )
        self.assertEqual(readiness.calls, 1)
        self.assertEqual(len(evidence.calls), 1)
        self.assertEqual(fabric.transform_calls, 1)

        result = await adapter.execute(
            **preview["required_next_tool"]["arguments"]
        )

        self.assertEqual(
            result["status"],
            "MOTION_COMPLETED_VISUALLY_CONFIRMED",
        )
        self.assertTrue(result["physical_motion_completed"])
        self.assertTrue(result["visual_motion_confirmed"])
        self.assertEqual(len(evidence.calls), 2)
        self.assertEqual(
            result["visual_verification"][
                "observed_displacement_world_m"
            ],
            [0.0, 0.0, 0.2],
        )

    async def test_declined_fixed_rig_creates_no_preview(self) -> None:
        client = _IntegratedClient()
        adapter = _adapter(
            client,
            require_visual_verification=True,
        )

        result = await adapter.preview(
            direction="UP",
            distance_m=0.1,
            arm_mount_assumption="CONFIRMED_X_FORWARD_Z_UP",
            fixed_vio_rig_assumption="REJECTED_OR_UNKNOWN",
        )

        self.assertEqual(
            result["status"],
            "VISUAL_VERIFICATION_DECLINED",
        )
        self.assertEqual(client.engage_count, 0)
        self.assertIsNone(client.snapshot["planning"]["last_preview"])

    async def test_camera_level_requires_confirmation(self) -> None:
        adapter = _adapter(_IntegratedClient())

        unconfirmed = await adapter.preview(
            direction="FRONT",
            distance_m=0.1,
            reference_frame="CAMERA_LEVEL",
        )
        self.assertEqual(
            unconfirmed["status"],
            "CAMERA_DIRECTION_CONFIRMATION_REQUIRED",
        )

        confirmed = await adapter.preview(
            direction="FRONT",
            distance_m=0.1,
            reference_frame="CAMERA_LEVEL",
            camera_level_assumption="CONFIRMED_GRAVITY_LEVELED",
        )
        self.assertEqual(confirmed["status"], "PREVIEW_READY")
        self.assertEqual(confirmed["reference_frame"], "CAMERA_LEVEL")

    async def test_north_is_not_an_alias_for_front(self) -> None:
        adapter = _adapter(_IntegratedClient())

        result = await adapter.preview(
            direction="NORTH",
            distance_m=0.1,
        )

        self.assertEqual(result["status"], "SURVEYED_FRAME_REQUIRED")
