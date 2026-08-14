from __future__ import annotations

import json
from pathlib import Path
import unittest
from types import SimpleNamespace

import numpy as np

from rebot_arm_integrated.contact import (
    TorqueBaseline,
    cartesian_wrench_to_joint_budget,
    force_position_ratios,
    isotropic_wrench_to_joint_budget,
    torque_limit_violations,
)
from rebot_arm_integrated.command_semantics import LatchedEndpointCommand, synchronized_velocity_limits
from rebot_arm_integrated.kinematics import ArmKinematics
from rebot_arm_integrated.hybrid import COMPLETE, MIT_SETTLE, POS_VEL_APPROACH, HybridApproachPolicy
from rebot_arm_integrated.modes import CONTACT_WORK, PRESS_MIT, TRANSIT_SPEED, MODE_SPECS
from rebot_arm_integrated.planning import (
    build_direct_preview,
    build_transit_frame_candidates,
    closest_collision_free_prefix,
    controller_owned_duration,
    joint_speed_policy_schedule,
    solve_cartesian_continuity,
    solve_cartesian_continuity_adaptive,
)
from rebot_arm_integrated.scene import SceneSnapshot, configuration_clearance
from rebot_arm_integrated.trajectory import QuinticJointSegment, TimedJointPath


INTEGRATED_ROOT = Path(__file__).resolve().parents[2]
BASIC_ROOT = INTEGRATED_ROOT.parent / "rebot_arm_dm"


class TrajectoryTests(unittest.TestCase):
    def test_joint_speed_policy_authenticates_above_ten_and_rejects_twenty(self):
        positions = [[0.0, 0.0, 0.0], [0.01, 0.0, 0.0]]
        schedule = joint_speed_policy_schedule(
            [[0.0] * 6, [0.75, 0.0, 0.0, 0.0, 0.0, 0.0]],
            positions,
            5.0,
            [5.0, 5.0, 5.0, 10.0, 10.0, 10.0],
            minimum_stage_duration_s=0.05,
            authentication_threshold_rad_s=10.0,
            hard_limit_rad_s=20.0,
        )
        self.assertGreater(schedule["requested_peak_joint_speed_rad_s"], 10.0)
        self.assertLess(schedule["requested_peak_joint_speed_rad_s"], 20.0)
        self.assertTrue(schedule["authentication_required"])
        self.assertFalse(schedule["hard_limit_exceeded"])
        self.assertLessEqual(schedule["effective_peak_joint_speed_rad_s"], 5.0)

    def test_linear_path_stretches_too_fast_request_to_provider_cap(self):
        schedule = joint_speed_policy_schedule(
            [[0.0] * 6, [0.35, 0.0, 0.0, 0.0, 0.0, 0.0]],
            [[0.0, 0.0, 0.0], [0.001, 0.0, 0.0]],
            10.0,
            [0.35, 0.35, 0.35, 0.5, 0.5, 0.5],
            minimum_stage_duration_s=0.01,
            authentication_threshold_rad_s=10.0,
            hard_limit_rad_s=20.0,
            command_rate_hz=50.0,
        )

        self.assertAlmostEqual(
            schedule["hardware_bounded_stage_durations_s"][0],
            1.0,
        )
        self.assertAlmostEqual(schedule["effective_stage_durations_s"][0], 1.0)
        self.assertAlmostEqual(
            schedule["effective_peak_joint_speed_rad_s"],
            0.35,
        )
        self.assertTrue(schedule["provider_or_motor_limited"])

    def test_joint_speed_policy_quantizes_each_stage_to_command_ticks(self):
        schedule = joint_speed_policy_schedule(
            [[0.0] * 6, [0.01, 0.0, 0.0, 0.0, 0.0, 0.0]],
            [[0.0, 0.0, 0.0], [0.001, 0.0, 0.0]],
            0.05,
            [0.35, 0.35, 0.35, 0.5, 0.5, 0.5],
            minimum_stage_duration_s=0.05,
            authentication_threshold_rad_s=10.0,
            hard_limit_rad_s=20.0,
            command_rate_hz=50.0,
        )

        self.assertEqual(schedule["effective_stage_command_ticks"], [3])
        self.assertEqual(schedule["effective_stage_durations_s"], [0.06])
        self.assertEqual(schedule["command_rate_hz"], 50.0)
        self.assertTrue(schedule["command_rate_quantized"])

    def test_timed_joint_path_interpolates_by_stage_duration(self):
        path = TimedJointPath.create(
            [[0.0, 0.0], [0.2, 0.1], [0.3, -0.1]],
            [0.4, 0.2],
        )

        first_q, first_qd, first_stage, first_progress = path.sample(0.2)
        self.assertTrue(np.allclose(first_q, [0.1, 0.05]))
        self.assertTrue(np.allclose(first_qd, [0.5, 0.25]))
        self.assertEqual(first_stage, 1)
        self.assertAlmostEqual(first_progress, 1.0 / 3.0)

        boundary_q, boundary_qd, boundary_stage, _ = path.sample(0.4)
        self.assertTrue(np.allclose(boundary_q, [0.2, 0.1]))
        self.assertTrue(np.allclose(boundary_qd, [0.5, -1.0]))
        self.assertEqual(boundary_stage, 2)

        final_q, final_qd, final_stage, final_progress = path.sample(1.0)
        self.assertTrue(np.allclose(final_q, [0.3, -0.1]))
        self.assertTrue(np.allclose(final_qd, [0.0, 0.0]))
        self.assertEqual(final_stage, 2)
        self.assertEqual(final_progress, 1.0)

    def test_quintic_segment_preserves_position_velocity_and_acceleration_boundaries(self):
        segment = QuinticJointSegment.create(
            [0.0, 0.2],
            [1.0, -0.1],
            2.0,
            qd0=[0.1, -0.2],
            qdd0=[0.2, 0.1],
        )
        q0, qd0, qdd0, p0 = segment.sample(0.0)
        q1, qd1, qdd1, p1 = segment.sample(2.0)
        self.assertTrue(np.allclose(q0, [0.0, 0.2]))
        self.assertTrue(np.allclose(qd0, [0.1, -0.2]))
        self.assertTrue(np.allclose(qdd0, [0.2, 0.1]))
        self.assertTrue(np.allclose(q1, [1.0, -0.1]))
        self.assertTrue(np.allclose(qd1, [0.0, 0.0], atol=1e-10))
        self.assertTrue(np.allclose(qdd1, [0.0, 0.0], atol=1e-10))
        self.assertEqual((p0, p1), (0.0, 1.0))

    def test_pos_vel_uses_a_latched_endpoint_instead_of_streaming_waypoints(self):
        limits = synchronized_velocity_limits(
            [0.0] * 6,
            [0.2, 0.1, 0.0, 0.0, 0.0, 0.0],
            2.0,
            [0.5] * 6,
            stationary_joint_limit_rad_s=0.02,
        )
        command = LatchedEndpointCommand.create(
            "POSITION_VELOCITY_LIMITED",
            [0.0] * 6,
            [0.2, 0.1, 0.0, 0.0, 0.0, 0.0],
            limits,
            keepalive_period_s=0.05,
        )
        first = command.commands()
        command.mark_sent(10.0)
        second = command.commands()
        self.assertEqual(first, second)
        self.assertFalse(command.should_send(10.01))
        self.assertTrue(command.should_send(10.06))
        self.assertEqual(command.snapshot()["strategy"], "LATCHED_ENDPOINT_KEEPALIVE")

    def test_pos_vel_saturates_requested_joint_speeds_at_provider_caps(self):
        limits = synchronized_velocity_limits(
            [0.0] * 6,
            [1.0, 0.2, 0.0, 0.0, 0.0, 0.0],
            0.25,
            [0.5, 0.4, 0.3, 0.2, 0.2, 0.2],
            stationary_joint_limit_rad_s=0.02,
        )
        self.assertTrue(np.allclose(limits, [0.5, 0.4, 0.02, 0.02, 0.02, 0.02]))

    def test_hybrid_handoff_is_stable_and_one_way(self):
        policy = HybridApproachPolicy.create(
            [0.1] * 6,
            handoff_position_error_rad=[0.05] * 6,
            handoff_velocity_rad_s=[0.1] * 6,
            completion_position_error_rad=[0.01] * 6,
            completion_velocity_rad_s=[0.03] * 6,
            required_stable_samples=3,
        )
        self.assertEqual(policy.update([0.0] * 6, [0.0] * 6), POS_VEL_APPROACH)
        for _ in range(3):
            phase = policy.update([0.08] * 6, [0.02] * 6)
        self.assertEqual(phase, MIT_SETTLE)
        self.assertEqual(policy.update([0.0] * 6, [1.0] * 6), MIT_SETTLE)
        for _ in range(3):
            phase = policy.update([0.1] * 6, [0.0] * 6)
        self.assertEqual(phase, COMPLETE)


class ContactTests(unittest.TestCase):
    def sample(self, torque_offset=0.0):
        return {
            "positions_rad": [0.0] * 7,
            "velocities_rad_s": [0.0] * 7,
            "torques_nm": [1.0 + torque_offset, 2.0, 3.0, 0.5, 0.4, 0.3, 0.0],
            "gravity_compensation": {"total_nm": [0.8, 1.8, 2.8, 0.3, 0.2, 0.1, 0.0]},
        }

    def test_baseline_tracks_gravity_change_and_detects_external_residual(self):
        samples = [self.sample(offset) for offset in (-0.02, -0.01, 0.0, 0.01, 0.02)]
        baseline = TorqueBaseline.from_samples(
            samples,
            maximum_velocity_rad_s=0.02,
            maximum_mad_nm=[0.1] * 6,
        )
        current_gravity = [0.9, 1.8, 2.8, 0.3, 0.2, 0.1]
        expected = baseline.expected_torque(current_gravity)
        residual = baseline.residual(expected + np.array([0.0, -0.4, 0.0, 0.0, 0.0, 0.0]), current_gravity)
        self.assertTrue(np.allclose(residual, [0.0, -0.4, 0.0, 0.0, 0.0, 0.0]))
        self.assertEqual(torque_limit_violations(residual, [0.2] * 6), [1])

    def test_force_position_budget_never_silently_clamps_to_a_small_value(self):
        ratios = force_position_ratios(
            [1.0] * 6,
            [1.0] * 6,
            [10.0] * 6,
            [0.5] * 6,
            margin_nm=[0.5] * 6,
        )
        self.assertTrue(np.allclose(ratios, [0.25] * 6))
        with self.assertRaises(ValueError):
            force_position_ratios(
                [4.0] * 6,
                [2.0] * 6,
                [10.0] * 6,
                [0.5] * 6,
                margin_nm=[0.5] * 6,
            )
        saturated = force_position_ratios(
            [4.0] * 6,
            [2.0] * 6,
            [10.0] * 6,
            [0.5] * 6,
            margin_nm=[0.5] * 6,
            saturate_at_caps=True,
        )
        self.assertTrue(np.allclose(saturated, [0.5] * 6))

    def test_cartesian_wrench_budget_maps_through_jacobian_transpose(self):
        jacobian = np.eye(6)
        budget = cartesian_wrench_to_joint_budget(
            jacobian,
            np.eye(3),
            [1.0, 2.0, 3.0],
            [0.4, 0.5, 0.6],
            minimum_joint_budget_nm=[0.1] * 6,
        )
        self.assertTrue(np.allclose(budget, [1.0, 2.0, 3.0, 0.4, 0.5, 0.6]))

    def test_isotropic_wrench_uses_vector_magnitude_not_per_axis_box(self):
        jacobian = np.eye(6)
        jacobian[:3, 0] = [1.0, 1.0, 1.0]
        budget = isotropic_wrench_to_joint_budget(
            jacobian,
            np.eye(3),
            2.0,
            0.5,
            minimum_joint_budget_nm=[0.1] * 6,
        )
        self.assertAlmostEqual(float(budget[0]), 2.0 * np.sqrt(3.0))


class ScenePlanningTests(unittest.TestCase):
    def setUp(self):
        model_path = BASIC_ROOT / "config" / "arm_model.json"
        if not model_path.exists():
            model_path = (
                BASIC_ROOT
                / "config_templates"
                / "arm_model.factory.json"
            )
        model = json.loads(model_path.read_text())
        self.kinematics = ArmKinematics(model)
        self.q = np.array([0.0, -0.18, -0.22, 0.12, 0.02, -0.05])

    def test_transit_candidates_use_only_direct_or_closest_safe_motion(self):
        start = np.eye(4)
        start[:3, 3] = [0.1, 0.0, 0.3]
        goal = np.eye(4)
        goal[:3, 3] = [0.3, 0.1, 0.25]

        candidates = build_transit_frame_candidates(
            start,
            goal,
        )

        names = [name for name, _frames in candidates]
        self.assertEqual(names, ["DIRECT"])
        self.assertTrue(np.array_equal(candidates[0][1][0], goal))

    def test_controller_duration_uses_requested_speed_and_joint_rate_caps(self):
        schedule = controller_owned_duration(
            [
                [0.0, 0.0],
                [0.3, 0.1],
                [0.5, 0.1],
            ],
            0.4,
            0.4,
            [0.2, 0.2],
            minimum_duration_s=0.25,
        )

        self.assertFalse(schedule["speed_clamped"])
        self.assertEqual(schedule["effective_speed_m_s"], 0.4)
        self.assertGreaterEqual(schedule["duration_s"], 3.75)
        self.assertEqual(
            schedule["limiting_factor"],
            "PROVIDER_JOINT_RATE_CAPS",
        )

    def test_semantic_scene_requires_explicit_contact_policy(self):
        points = self.kinematics.evaluate(self.q).points
        center = ((points[1] + points[2]) / 2.0).tolist()
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "scene-1",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {"sphere_id": "s1", "object_id": "workpiece", "center_m": center, "radius_m": 0.1, "type": "WORK_OBJECT"}
                ],
            }
        )
        blocked = configuration_clearance(points, scene, [0.04] * 7)
        allowed = configuration_clearance(points, scene, [0.04] * 7, allowed_contact_object_ids={"workpiece"})
        self.assertFalse(blocked["collision_free"])
        self.assertTrue(allowed["collision_free"])

    def test_collision_diagnostics_are_bounded_without_losing_blocking_state(self):
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "many-collisions",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {
                        "sphere_id": f"sphere-{index}",
                        "object_id": f"obstacle-{index}",
                        "center_m": [0.0, 0.0, 0.05],
                        "radius_m": 0.06,
                        "type": "KEEP_OUT",
                    }
                    for index in range(20)
                ],
            }
        )
        points = [[0.0, 0.0, index * 0.1] for index in range(8)]

        report = configuration_clearance(
            points,
            scene,
            [0.04] * 7,
            maximum_collision_details=2,
        )

        self.assertFalse(report["collision_free"])
        self.assertGreater(report["collision_count"], 2)
        self.assertEqual(len(report["collisions"]), 2)
        self.assertTrue(report["collision_details_truncated"])

    def test_semantic_clearance_margins_are_type_specific(self):
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        margins = {
            "KEEP_OUT": 0.01,
            "PUSHABLE": 0.0,
            "WORK_OBJECT": 0.0,
        }

        def scene_for(object_type: str) -> SceneSnapshot:
            return SceneSnapshot.from_payload(
                {
                    "scene_revision": f"margin-{object_type}",
                    "frame_id": "rebot_arm_base",
                    "spheres": [
                        {
                            "sphere_id": object_type.lower(),
                            "object_id": object_type.lower(),
                            "center_m": [0.5, 0.055, 0.0],
                            "radius_m": 0.04,
                            "type": object_type,
                        }
                    ],
                }
            )

        keep_out = configuration_clearance(
            points,
            scene_for("KEEP_OUT"),
            [0.01],
            clearance_margin_by_type_m=margins,
        )
        work_object = configuration_clearance(
            points,
            scene_for("WORK_OBJECT"),
            [0.01],
            clearance_margin_by_type_m=margins,
        )
        pushable = configuration_clearance(
            points,
            scene_for("PUSHABLE"),
            [0.01],
            permit_pushable_contact=True,
            clearance_margin_by_type_m=margins,
        )

        self.assertFalse(keep_out["collision_free"])
        self.assertAlmostEqual(
            keep_out["collisions"][0]["raw_clearance_m"],
            0.005,
        )
        self.assertAlmostEqual(
            keep_out["collisions"][0][
                "required_clearance_margin_m"
            ],
            0.01,
        )
        self.assertTrue(work_object["collision_free"])
        self.assertTrue(pushable["collision_free"])
        self.assertIsNone(pushable["minimum_clearance_m"])

    def test_mounted_effector_sphere_uses_the_same_semantic_margins(self):
        points = [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0]]
        robot_sphere = {
            "primitive_id": "gripper-body",
            "center_m": [0.5, 0.0, 0.0],
            "radius_m": 0.03,
        }

        def scene_for(object_type: str, center_x: float) -> SceneSnapshot:
            return SceneSnapshot.from_payload(
                {
                    "scene_revision": f"effector-margin-{object_type}",
                    "frame_id": "rebot_arm_base",
                    "spheres": [
                        {
                            "sphere_id": object_type.lower(),
                            "object_id": object_type.lower(),
                            "center_m": [center_x, 0.0, 0.0],
                            "radius_m": 0.01,
                            "type": object_type,
                        }
                    ],
                }
            )

        margins = {"KEEP_OUT": 0.01, "PUSHABLE": 0.0, "WORK_OBJECT": 0.0}
        keep_out = configuration_clearance(
            points,
            scene_for("KEEP_OUT", 0.545),
            [0.01],
            robot_spheres=[robot_sphere],
            clearance_margin_by_type_m=margins,
        )
        work_object = configuration_clearance(
            points,
            scene_for("WORK_OBJECT", 0.545),
            [0.01],
            robot_spheres=[robot_sphere],
            clearance_margin_by_type_m=margins,
        )

        self.assertFalse(keep_out["collision_free"])
        self.assertEqual(
            keep_out["collisions"][0]["robot_primitive_id"],
            "gripper-body",
        )
        self.assertAlmostEqual(keep_out["minimum_clearance_m"], -0.005)
        self.assertTrue(work_object["collision_free"])
        self.assertAlmostEqual(work_object["minimum_clearance_m"], 0.005)

    def test_preview_transforms_mounted_effector_spheres_with_controlled_frame(self):
        tool_to_control = np.eye(4)
        controlled = self.kinematics.controlled_frame(
            self.q,
            tool_to_control,
        )
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "effector-sphere-preview",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {
                        "sphere_id": "tip-obstacle",
                        "object_id": "tip-obstacle",
                        "center_m": controlled[:3, 3].tolist(),
                        "radius_m": 0.001,
                        "type": "WORK_OBJECT",
                    }
                ],
            }
        )

        preview = build_direct_preview(
            self.kinematics,
            self.q,
            self.q,
            0.5,
            scene=scene,
            link_radii_m=[0.001] * 6,
            tool_to_control=tool_to_control,
            effector_spheres=[
                {
                    "primitive_id": "gripper-tip",
                    "translation_m": [0.0, 0.0, 0.0],
                    "radius_m": 0.005,
                }
            ],
        )

        self.assertFalse(preview.collision_free)
        self.assertEqual(
            preview.collisions[0]["robot_primitive_id"],
            "gripper-tip",
        )

    def test_collision_preview_can_be_truncated_to_a_safe_prefix(self):
        q_goal = self.q.copy()
        q_goal[0] += 0.4
        goal_tool = self.kinematics.evaluate(q_goal).points[-1]
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "closest-safe-prefix",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {
                        "sphere_id": "goal-obstacle",
                        "object_id": "goal-obstacle",
                        "center_m": goal_tool.tolist(),
                        "radius_m": 0.02,
                        "type": "KEEP_OUT",
                    }
                ],
            }
        )
        margins = {
            "KEEP_OUT": 0.01,
            "PUSHABLE": 0.0,
            "WORK_OBJECT": 0.0,
        }
        preview = build_direct_preview(
            self.kinematics,
            self.q,
            q_goal,
            1.0,
            scene=scene,
            link_radii_m=[0.01] * 7,
            clearance_margin_by_type_m=margins,
        )

        prefix = closest_collision_free_prefix(
            self.kinematics,
            preview,
            scene=scene,
            link_radii_m=[0.01] * 7,
            clearance_margin_by_type_m=margins,
        )

        self.assertFalse(preview.collision_free)
        self.assertIsNotNone(preview.first_collision_sample_index)
        self.assertGreaterEqual(len(prefix), 2)
        safe_endpoint = build_direct_preview(
            self.kinematics,
            prefix[-2],
            prefix[-1],
            0.1,
            scene=scene,
            link_radii_m=[0.01] * 7,
            clearance_margin_by_type_m=margins,
        )
        self.assertTrue(safe_endpoint.collision_free)

    def test_canonical_scene_enforces_roi_and_minimum_sphere_radius(self):
        scene = SceneSnapshot.from_payload(
            {
                "contract_version": 2,
                "scene_revision": "scene-canonical-1",
                "frame_id": "rebot_arm_base",
                "roi_layers": [
                    {
                        "scope": "GRIPPER_0P5M",
                        "center_m": [0.4, 0.0, 0.3],
                        "radius_m": 0.5,
                        "minimum_sphere_radius_m": 0.02,
                    },
                    {
                        "scope": "ARM_BASE_1P2M",
                        "center_m": [0.0, 0.0, 0.0],
                        "radius_m": 1.2,
                        "minimum_sphere_radius_m": 0.06,
                    },
                ],
                "spheres": [
                    {
                        "sphere_id": "workpiece-1",
                        "object_id": "toilet-paper",
                        "center_m": [0.45, 0.0, 0.3],
                        "radius_m": 0.02,
                        "type": "WORKPIECE",
                        "roi_scope": "GRIPPER_0P5M",
                    },
                    {
                        "sphere_id": "obstacle-1",
                        "object_id": "obstacle-1",
                        "center_m": [0.8, 0.0, 0.0],
                        "radius_m": 0.06,
                        "roi_scope": "ARM_BASE_1P2M",
                    },
                ],
            }
        )

        self.assertEqual(scene.contract_version, 2)
        self.assertEqual(scene.spheres[0].object_type, "WORK_OBJECT")
        self.assertEqual(scene.spheres[1].object_type, "KEEP_OUT")

        with self.assertRaisesRegex(ValueError, "minimum radius"):
            SceneSnapshot.from_payload(
                {
                    "contract_version": 2,
                    "scene_revision": "scene-too-small",
                    "frame_id": "rebot_arm_base",
                    "roi_layers": [
                        {
                            "scope": "ARM_BASE_1P2M",
                            "center_m": [0.0, 0.0, 0.0],
                            "radius_m": 1.2,
                            "minimum_sphere_radius_m": 0.06,
                        }
                    ],
                    "spheres": [
                        {
                            "sphere_id": "tiny",
                            "center_m": [0.2, 0.0, 0.0],
                            "radius_m": 0.05,
                            "roi_scope": "ARM_BASE_1P2M",
                        }
                    ],
                }
            )

    def test_direct_preview_reports_collision_without_executing_commands(self):
        tool = self.kinematics.evaluate(self.q).points[-1]
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "scene-2",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {"sphere_id": "keep", "object_id": "keep", "center_m": tool.tolist(), "radius_m": 0.05, "type": "KEEP_OUT"}
                ],
            }
        )
        preview = build_direct_preview(
            self.kinematics,
            self.q,
            self.q + np.array([0.01, 0.0, 0.0, 0.0, 0.0, 0.0]),
            1.0,
            scene=scene,
            link_radii_m=[0.04] * 7,
        )
        self.assertFalse(preview.collision_free)
        self.assertTrue(preview.collisions)

    def test_collision_polyline_ends_at_selected_controlled_frame(self):
        tool_to_control = np.eye(4, dtype=float)
        tool_to_control[2, 3] = 0.25
        controlled = self.kinematics.controlled_frame(
            self.q,
            tool_to_control,
        )[:3, 3]
        scene = SceneSnapshot.from_payload(
            {
                "scene_revision": "scene-controlled-frame-offset",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {
                        "sphere_id": "controlled-endpoint",
                        "object_id": "controlled-endpoint",
                        "center_m": controlled.tolist(),
                        "radius_m": 0.01,
                        "type": "KEEP_OUT",
                    }
                ],
            }
        )

        preview = build_direct_preview(
            self.kinematics,
            self.q,
            self.q + np.array([0.001, 0.0, 0.0, 0.0, 0.0, 0.0]),
            0.5,
            scene=scene,
            link_radii_m=[0.01] * 7,
            tool_to_control=tool_to_control,
        )

        self.assertFalse(preview.collision_free)
        self.assertTrue(
            any(
                collision["sphere_id"] == "controlled-endpoint"
                for collision in preview.collisions
            )
        )

    def test_three_modes_map_to_distinct_basic_backends(self):
        self.assertEqual(
            {MODE_SPECS[name].basic_mode for name in (TRANSIT_SPEED, CONTACT_WORK, PRESS_MIT)},
            {"POSITION_VELOCITY_LIMITED", "POSITION_EFFORT_LIMITED", "IMPEDANCE"},
        )

    def test_cartesian_continuity_uses_previous_waypoint_as_the_next_ik_seed(self):
        start = np.eye(4)
        goal = np.eye(4)
        goal[0, 3] = 0.2
        seeds = []

        def solve(seed, target):
            seeds.append(seed.copy())
            q_goal = seed.copy()
            q_goal[0] = target[0, 3]
            return SimpleNamespace(
                q_goal=q_goal,
                sigma_min=0.05,
                position_residual_m=0.0,
                orientation_residual_rad=0.0,
                iterations=2,
            )

        result = solve_cartesian_continuity([0.0] * 6, start, goal, solve, waypoint_count=4)
        self.assertEqual(len(result.q_waypoints), 5)
        self.assertTrue(np.allclose(seeds[1], result.q_waypoints[1]))
        self.assertAlmostEqual(result.maximum_waypoint_joint_step_rad, 0.05)
        self.assertAlmostEqual(result.minimum_sigma, 0.05)

    def test_adaptive_continuity_subdivides_until_joint_steps_are_bounded(self):
        start = np.eye(4)
        goal = np.eye(4)
        goal[0, 3] = 0.4

        def solve(seed, target):
            q_goal = seed.copy()
            q_goal[0] = target[0, 3]
            return SimpleNamespace(
                q_goal=q_goal,
                sigma_min=0.05,
                position_residual_m=0.0,
                orientation_residual_rad=0.0,
                iterations=2,
            )

        result = solve_cartesian_continuity_adaptive(
            [0.0] * 6,
            start,
            goal,
            solve,
            initial_waypoint_count=2,
            maximum_waypoint_count=16,
            maximum_joint_step_rad=0.06,
        )

        self.assertEqual(len(result.q_waypoints), 9)
        self.assertLessEqual(result.maximum_waypoint_joint_step_rad, 0.06)


if __name__ == "__main__":
    unittest.main()
