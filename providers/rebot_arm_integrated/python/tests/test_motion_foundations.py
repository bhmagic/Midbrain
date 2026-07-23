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
from rebot_arm_integrated.planning import build_direct_preview, solve_cartesian_continuity
from rebot_arm_integrated.scene import SceneSnapshot, configuration_clearance
from rebot_arm_integrated.trajectory import QuinticJointSegment


INTEGRATED_ROOT = Path(__file__).resolve().parents[2]
BASIC_ROOT = INTEGRATED_ROOT.parent / "rebot_arm_dm"


class TrajectoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
