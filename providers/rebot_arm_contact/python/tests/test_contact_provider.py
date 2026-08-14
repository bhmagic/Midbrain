from __future__ import annotations

from pathlib import Path
from unittest import mock
import copy
import json
import math
import threading
import time
import unittest

import numpy as np

from rebot_arm_contact.authorization import (
    AuthorizationError,
    canonical_sha256,
    sign_assertion,
    verify_assertion,
)
from rebot_arm_contact.authority_state import evaluate_authority_coordination
from rebot_arm_contact.basic_client import BasicLease
from rebot_arm_contact.controller import ContactController
from rebot_arm_contact.kinematics import ContactKinematics


PROVIDER_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROVIDER_ROOT.parents[1]
SECRET = "contact-test-secret-with-at-least-32-bytes"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def quaternion_from_matrix(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=float)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        return [
            float((matrix[2, 1] - matrix[1, 2]) / scale),
            float((matrix[0, 2] - matrix[2, 0]) / scale),
            float((matrix[1, 0] - matrix[0, 1]) / scale),
            float(0.25 * scale),
        ]
    index = int(np.argmax(np.diag(matrix)))
    if index == 0:
        scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
        return [
            float(0.25 * scale),
            float((matrix[0, 1] + matrix[1, 0]) / scale),
            float((matrix[0, 2] + matrix[2, 0]) / scale),
            float((matrix[2, 1] - matrix[1, 2]) / scale),
        ]
    if index == 1:
        scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
        return [
            float((matrix[0, 1] + matrix[1, 0]) / scale),
            float(0.25 * scale),
            float((matrix[1, 2] + matrix[2, 1]) / scale),
            float((matrix[0, 2] - matrix[2, 0]) / scale),
        ]
    scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
    return [
        float((matrix[0, 2] + matrix[2, 0]) / scale),
        float((matrix[1, 2] + matrix[2, 1]) / scale),
        float(0.25 * scale),
        float((matrix[1, 0] - matrix[0, 1]) / scale),
    ]


class FakeBasic:
    def __init__(self):
        self.model_value = load_json(
            WORKSPACE_ROOT
            / "providers/rebot_arm_dm/config_templates/arm_model.factory.json"
        )
        self.model_value["command_limits"] = {
            "POSITION_EFFORT_LIMITED": [
                {
                    "joint_index": index,
                    "joint_name": joint["name"],
                    "velocity_limit_rad_s": 4.0 if index < 6 else 2.1,
                    "torque_limit_nm": float(
                        joint["motor_limits"]["configured_tmax_nm"]
                    )
                    * float(
                        self.model_value["control"][
                            "physical_test_pos_tor_ratio_cap"
                        ][index]
                    ),
                }
                for index, joint in enumerate(self.model_value["joints"])
            ]
        }
        names = [joint["name"] for joint in self.model_value["joints"][:6]]
        self.assembly_value = {
            "schema": "midbrain.robot_assembly_state",
            "schema_version": 1,
            "assembly_fingerprint": "a" * 64,
            "qualified_control_roles": {
                "free_space": None,
                "contact": {
                    "required_capability": "robot_arm.motion.contact.position_effort_limited.v1",
                    "qualification": "DEVELOPMENT",
                },
                "grip": None,
            },
            "resource_groups": [
                {
                    "group_id": "arm",
                    "resource_id": "robot_arm.primary/arm",
                    "joint_names": names,
                }
            ],
            "mounted_effector": {
                "profile_revision": "rebot-b601-dm-5-inch-blade-v3",
                "controlled_frame": {
                    "frame_id": "rebot_arm_knife_tip",
                    "transform": {
                        "translation_m": [0.0, 0.0, 0.0],
                        "rpy_rad": [0.0, 0.0, 0.0],
                    },
                },
            },
        }
        positions = [
            float(joint.get("home_position_rad", 0.0))
            for joint in self.model_value["joints"][:6]
        ]
        self.state_value = {
            "ready": True,
            "feedback_age_ms": 1.0,
            "positions_rad": positions + [0.0],
            "velocities_rad_s": [0.0] * 7,
            "torques_nm": [0.0] * 7,
            "temperatures_c": [None] * 7,
            "gravity_compensation": {"total_nm": [2.0] * 6 + [0.0]},
            "active_command_modes": ["POSITION_EFFORT_LIMITED"] * 6 + [None],
            "float_transition_pending_joint_indices": [],
        }
        self.resource_id = None
        self.lease = None
        self.commands: list[list[dict]] = []
        self.float_calls = 0
        self.fail_command = False
        self.state_call_threads: list[str] = []
        self.renew_calls = 0

    def model(self):
        return copy.deepcopy(self.model_value)

    def assembly(self):
        return copy.deepcopy(self.assembly_value)

    def state(self):
        self.state_call_threads.append(threading.current_thread().name)
        return copy.deepcopy(self.state_value)

    def bind_resource(self, resource_id):
        self.resource_id = resource_id

    def acquire(self, holder, duration_ms):
        self.lease = BasicLease(
            "lease-1", 3, time.monotonic() + duration_ms / 1000.0, holder, self.resource_id
        )
        return copy.deepcopy(self.lease)

    def renew(self, duration_ms):
        self.renew_calls += 1
        self.lease.expires_monotonic = time.monotonic() + duration_ms / 1000.0
        return copy.deepcopy(self.lease)

    def lease_snapshot(self):
        return copy.deepcopy(self.lease)

    def command(self, commands, timeout_ms):
        if self.fail_command:
            raise RuntimeError("simulated Basic command failure")
        self.commands.append(copy.deepcopy(commands))
        return {"accepted": True}

    def float(self, reason):
        self.float_calls += 1
        self.state_value["active_command_modes"] = ["IMPEDANCE"] * 6 + [None]
        return {"accepted": True}

    def release(self, reason):
        self.lease = None


def provider_config() -> dict:
    return load_json(PROVIDER_ROOT / "config_templates/controller.default.json")


def plan_for(controller: ContactController, *, rotational=False, steps=1) -> dict:
    plan_steps = []
    for sequence in range(steps):
        plan_steps.append(
            {
                "sequence": sequence,
                "motion_type": "ONE_SHOT",
                "target": {
                    "frame_id": controller.kinematics.root_frame_id,
                    "position_mode": "ABSOLUTE_ROOT",
                    "position_m": [0.30 + sequence * 0.01, 0.0, 0.25],
                    "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                },
                "wrench": {
                    "frame_id": controller.acting_frame_id,
                    "force_n": [0.0, 0.0, 5.0],
                    "torque_nm": [0.2, 0.1, 0.3] if rotational else [0.0, 0.0, 0.0],
                },
                "locked_joint_names": [controller.kinematics.joint_names[0]],
                "delay_after_accept_s": 0.0,
                "next_command_timeout_s": 6.0,
            }
        )
    return {
        "schema": "midbrain.contact_work_plan",
        "schema_version": 1,
        "plan_id": "plan-1",
        "skill_id": "contact.slicing",
        "execution_id": "execution-1",
        "provider_id": controller.provider_id,
        "assembly_fingerprint": controller.assembly_fingerprint,
        "acting_frame_id": controller.acting_frame_id,
        "manager_authority": {
            "resource_id": controller.arm_resource_id,
            "lease_id": "manager-lease-1",
            "owner_id": "execution-1",
            "fencing_generation": 4,
            "permissions": ["execute_contact", "relax"],
        },
        "steps": plan_steps,
    }


def assertion_for(controller: ContactController, plan: dict) -> str:
    now = time.time_ns() // 1000
    payload = {
        "schema": "midbrain.contact_work_authorization",
        "schema_version": 1,
        "assertion_id": "assertion-1",
        "nonce": "0123456789abcdef0123456789abcdef",
        "issuer_skill_id": plan["skill_id"],
        "execution_id": plan["execution_id"],
        "audience_provider_id": controller.provider_id,
        "provider_instance_id": controller.provider_instance_id,
        "provider_boot_id": controller.provider_boot_id,
        "assembly_fingerprint": controller.assembly_fingerprint,
        "mounted_effector_revision": controller.mounted_effector_revision,
        "plan_sha256": canonical_sha256(plan),
        "issued_at_us": now,
        "expires_at_us": now + 30_000_000,
    }
    return sign_assertion(payload, SECRET)


class AuthorizationTests(unittest.TestCase):
    def test_assertion_verifies_and_detects_mismatch(self):
        payload = {
            "schema": "midbrain.contact_work_authorization",
            "schema_version": 1,
            "assertion_id": "a",
            "nonce": "0123456789abcdef",
            "mounted_effector_revision": "v3",
            "issued_at_us": 100,
            "expires_at_us": 500,
            "plan_sha256": "b" * 64,
        }
        token = sign_assertion(payload, SECRET)
        verified = verify_assertion(
            token,
            SECRET,
            expected={"plan_sha256": "b" * 64},
            now_us=200,
        )
        self.assertEqual(verified["assertion_id"], "a")
        with self.assertRaises(AuthorizationError):
            verify_assertion(
                token,
                SECRET,
                expected={"plan_sha256": "c" * 64},
                now_us=200,
            )


class KinematicsTests(unittest.TestCase):
    def test_force_and_rotational_torque_add_linearly_through_jacobian(self):
        model = load_json(
            WORKSPACE_ROOT
            / "providers/rebot_arm_dm/config_templates/arm_model.factory.json"
        )
        kinematics = ContactKinematics(model, np.eye(4))
        q = [float(joint.get("home_position_rad", 0.0)) for joint in model["joints"][:6]]
        force_only = kinematics.joint_wrench(
            q, [2.0, -1.0, 4.0], [0.0, 0.0, 0.0], kinematics.root_frame_id, "acting"
        )
        torque_only = kinematics.joint_wrench(
            q, [0.0, 0.0, 0.0], [0.2, 0.3, -0.1], kinematics.root_frame_id, "acting"
        )
        combined = kinematics.joint_wrench(
            q, [2.0, -1.0, 4.0], [0.2, 0.3, -0.1], kinematics.root_frame_id, "acting"
        )
        self.assertTrue(np.allclose(combined, force_only + torque_only))

    def test_locked_joint_pose_solver_preserves_six_dof_target(self):
        model = load_json(
            WORKSPACE_ROOT
            / "providers/rebot_arm_dm/config_templates/arm_model.factory.json"
        )
        kinematics = ContactKinematics(model, np.eye(4))
        target_q = np.asarray([0.3, -0.7, -0.8, 0.4, -0.3, 0.2])
        seed_q = np.asarray([-0.2, -0.35, -0.55, -0.1, 0.15, 0.2])
        target_transform = kinematics.evaluate(target_q).controlled_transform
        result = kinematics.solve_pose(
            seed_q,
            {
                "position_m": target_transform[:3, 3].tolist(),
                "orientation_xyzw": quaternion_from_matrix(
                    target_transform[:3, :3]
                ),
            },
            {5: float(target_q[5])},
            maximum_iterations=180,
            damping=0.01,
            maximum_step_rad=0.10,
            joint_margin_rad=0.001,
            orientation_weight_m_per_rad=0.5,
        )
        self.assertAlmostEqual(result.q_goal[5], target_q[5], places=12)
        self.assertLess(result.position_residual_m, 1e-5)
        self.assertLess(result.orientation_residual_rad, 1e-4)


class AuthorityCoordinationTests(unittest.TestCase):
    def test_signed_upstream_lineage_matches_live_manager_record(self):
        authority = {
            "resource_id": "robot_arm.primary/arm",
            "lease_id": "manager-lease",
            "owner_id": "execution",
            "fencing_generation": 8,
            "permissions": ["execute_contact", "relax"],
        }
        result = evaluate_authority_coordination(
            resource_id="robot_arm.primary/arm",
            manager_available=True,
            manager_view={
                "resource_id": "robot_arm.primary/arm",
                "active_lease": authority,
            },
            local_basic_lease={"lease_id": "basic", "fencing_generation": 19},
            upstream_authority=authority,
            local_writer_active=True,
            motion_inhibited=False,
        )
        self.assertEqual(result["state"], "COORDINATED_ACTIVE")
        self.assertTrue(result["lineage"]["bound"])
        self.assertFalse(result["fencing"]["numeric_equality_has_meaning"])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.basic = FakeBasic()
        self.controller = ContactController(
            provider_config(),
            self.basic,
            provider_instance_id="provider-instance",
            provider_boot_id="provider-boot",
        )
        self.controller._refresh_runtime_binding()
        self.controller.enter_hot()

    def test_new_step_uses_basic_limits_and_locked_joint_uses_full_torque(self):
        plan = plan_for(self.controller, steps=2)
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            measured_before = np.asarray(
                self.basic.state_value["positions_rad"][:6],
                dtype=float,
            )
            first = self.controller.move("execution-1", 0)
            captured_lock_position = first["target_joint_positions_rad"][0]
            self.basic.state_value["positions_rad"][0] += 0.5
            second = self.controller.move("execution-1", 1)
        self.assertEqual(first["disposition"], "ACCEPTED")
        expected_transition_s = float(
            np.max(
                np.abs(
                    np.asarray(first["target_joint_positions_rad"])
                    - measured_before
                )
                / np.asarray([4.0, 4.0, 4.0, 4.0, 4.0, 4.0])
            )
        )
        self.assertAlmostEqual(
            first["velocity_limited_transition_time_s"],
            expected_transition_s,
        )
        self.assertEqual(second["disposition"], "SUPERSEDED")
        self.assertEqual(len(self.basic.commands), 2)
        last_commands = self.basic.commands[-1]
        self.assertEqual(
            second["target_joint_positions_rad"][0], captured_lock_position
        )
        self.assertEqual(last_commands[0]["values"]["torque_limit_nm"], 27.0)
        self.assertTrue(
            all(command["mode"] == "POSITION_EFFORT_LIMITED" for command in last_commands)
        )

    def test_nonzero_rotational_components_are_accepted_without_gate(self):
        plan = plan_for(self.controller, rotational=True)
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            result = self.controller.move("execution-1", 0)
        self.assertEqual(result["disposition"], "ACCEPTED")
        self.assertTrue(np.any(self.controller.wrench_budget_nm > 0.0))

    def test_relative_target_resolves_from_fresh_measured_effector_position(self):
        current_q = np.asarray(self.basic.state_value["positions_rad"][:6], dtype=float)
        current_pose = self.controller.kinematics.evaluate(
            current_q
        ).controlled_transform
        plan = plan_for(self.controller)
        plan["steps"][0]["locked_joint_names"] = []
        plan["steps"][0]["target"]["position_mode"] = (
            "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES"
        )
        plan["steps"][0]["target"]["position_m"] = [0.0, 0.0, 0.02]
        plan["steps"][0]["target"]["orientation_xyzw"] = quaternion_from_matrix(
            current_pose[:3, :3]
        )
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            result = self.controller.move("execution-1", 0)

        self.assertEqual(
            result["signed_position_mode"],
            "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
        )
        np.testing.assert_allclose(result["signed_position_m"], [0.0, 0.0, 0.02])
        np.testing.assert_allclose(
            result["resolved_target_position_m"],
            current_pose[:3, 3] + np.asarray([0.0, 0.0, 0.02]),
        )

    def test_cartesian_segment_streams_changing_setpoints_at_basic_rate(self):
        config = provider_config()
        config.pop("trajectory")
        controller = ContactController(
            config,
            self.basic,
            provider_instance_id="segment-instance",
            provider_boot_id="segment-boot",
        )
        controller.start()
        controller.enter_hot()
        current_q = np.asarray(self.basic.state_value["positions_rad"][:6], dtype=float)
        current_pose = controller.kinematics.evaluate(current_q).controlled_transform
        plan = plan_for(controller)
        plan["execution_id"] = "segment-execution"
        plan["manager_authority"]["owner_id"] = "segment-execution"
        plan["steps"][0]["motion_type"] = "CARTESIAN_SEGMENT"
        plan["steps"][0]["locked_joint_names"] = []
        plan["steps"][0]["target"]["position_m"] = (
            current_pose[:3, 3] + np.array([0.01, 0.0, 0.0])
        ).tolist()
        plan["steps"][0]["target"]["orientation_xyzw"] = quaternion_from_matrix(
            current_pose[:3, :3]
        )
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            controller.begin_session(plan, assertion_for(controller, plan))
            result = controller.move("segment-execution", 0)
            deadline = time.monotonic() + 1.0
            while len(self.basic.commands) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
        try:
            self.assertEqual(result["motion_type"], "CARTESIAN_SEGMENT")
            self.assertEqual(result["cartesian_segment"]["stream_rate_hz"], 50.0)
            self.assertGreaterEqual(result["cartesian_segment"]["ik_waypoint_count"], 5)
            self.assertLessEqual(
                result["cartesian_segment"]["target_waypoint_spacing_m"],
                0.002,
            )
            self.assertGreaterEqual(len(self.basic.commands), 4)
            joint_targets = [
                tuple(command["values"]["position_rad"] for command in envelope)
                for envelope in self.basic.commands
            ]
            self.assertGreater(len(set(joint_targets)), 1)
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["basic_control_rate_hz"], 50.0)
            self.assertEqual(snapshot["motion_type"], "CARTESIAN_SEGMENT")
            self.assertGreater(snapshot["cartesian_segment"]["command_updates_sent"], 0)
            self.assertNotIn(
                "contact-work-control",
                self.basic.state_call_threads,
            )
            tracking = snapshot["cartesian_segment"]["tracking_observation"]
            self.assertGreater(tracking["measured_samples"], 0)
            self.assertIn("maximum_measured_cross_track_error_m", tracking)
            self.assertIn("maximum_measured_joint_tracking_error_rad", tracking)
            self.assertIn("NOT_TASK_SUCCESS", tracking["semantics"])
        finally:
            controller.stop()

    def test_feedback_poll_failure_before_first_setpoint_does_not_end_session(self):
        self.controller.start()
        try:
            plan = plan_for(self.controller)
            with mock.patch.dict(
                "os.environ",
                {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET},
                clear=False,
            ):
                self.controller.begin_session(
                    plan,
                    assertion_for(self.controller, plan),
                )

            self.basic.state_value["feedback_age_ms"] = 1000.0
            deadline = time.monotonic() + 0.5
            while self.controller.ready and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertFalse(self.controller.ready)
            self.assertEqual(
                self.controller.snapshot()["session_id"],
                "execution-1",
            )
            self.assertEqual(
                self.controller.snapshot()["control_state"],
                "WAITING_FOR_FIRST_SETPOINT",
            )
        finally:
            self.controller.stop()

    def test_long_cartesian_ik_planning_services_basic_lease(self):
        config = provider_config()
        config["basic"]["lease_renewal_interval_ms"] = 10
        controller = ContactController(
            config,
            self.basic,
            provider_instance_id="planning-instance",
            provider_boot_id="planning-boot",
        )
        controller._refresh_runtime_binding()
        controller.enter_hot()
        current_q = np.asarray(
            self.basic.state_value["positions_rad"][:6],
            dtype=float,
        )
        current_pose = controller.kinematics.evaluate(
            current_q
        ).controlled_transform
        plan = plan_for(controller)
        plan["execution_id"] = "slow-planning-execution"
        plan["manager_authority"]["owner_id"] = "slow-planning-execution"
        plan["steps"][0]["motion_type"] = "CARTESIAN_SEGMENT"
        plan["steps"][0]["locked_joint_names"] = []
        plan["steps"][0]["target"]["position_m"] = (
            current_pose[:3, 3] + np.array([0.01, 0.0, 0.0])
        ).tolist()
        plan["steps"][0]["target"]["orientation_xyzw"] = (
            quaternion_from_matrix(current_pose[:3, :3])
        )
        original_solve = controller._solve_pose

        def slow_solve(*args, **kwargs):
            time.sleep(0.02)
            return original_solve(*args, **kwargs)

        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            controller.begin_session(
                plan,
                assertion_for(controller, plan),
            )
            with mock.patch.object(
                controller,
                "_solve_pose",
                side_effect=slow_solve,
            ):
                controller.move("slow-planning-execution", 0)

        self.assertGreaterEqual(self.basic.renew_calls, 1)

    def test_manager_authority_lineage_must_target_the_arm_group(self):
        plan = plan_for(self.controller)
        plan["manager_authority"]["resource_id"] = "robot_arm.primary/gripper"
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            with self.assertRaisesRegex(ValueError, "another resource"):
                self.controller.begin_session(plan, assertion_for(self.controller, plan))

    def test_failed_basic_command_does_not_accept_or_advance_endpoint(self):
        plan = plan_for(self.controller)
        self.basic.fail_command = True
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            with self.assertRaisesRegex(RuntimeError, "simulated Basic command failure"):
                self.controller.move("execution-1", 0)
        self.assertIsNone(self.controller.endpoint)
        self.assertEqual(self.controller.session.active_sequence, -1)
        self.assertEqual(self.controller.lock_positions, {})

    def test_unreachable_cartesian_target_is_best_effort_not_rejected(self):
        plan = plan_for(self.controller)
        plan["steps"][0]["target"]["position_m"] = [20.0, 20.0, 20.0]
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            result = self.controller.move("execution-1", 0)
        self.assertGreater(result["position_residual_m"], 1.0)
        self.assertFalse(result["cartesian_arrival_required"])

    def test_relax_transitions_to_impedance_and_releases_lease(self):
        plan = plan_for(self.controller)
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            self.controller.move("execution-1", 0)
            result = self.controller.relax("execution-1")
        self.assertTrue(result["float_confirmed"])
        self.assertIsNone(self.basic.lease)
        self.assertIsNone(self.controller.session)

    def test_active_session_attempts_float_after_local_lease_is_lost(self):
        plan = plan_for(self.controller)
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(plan, assertion_for(self.controller, plan))
            self.controller.move("execution-1", 0)
            self.basic.lease = None
            confirmed = self.controller._relax("lease lost", "LEASE_LOST_RELAXED")
        self.assertTrue(confirmed)
        self.assertEqual(self.basic.float_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            self.controller.move("execution-1", 1)
        self.assertEqual(
            self.controller.snapshot()["last_relax_reason"],
            "lease lost",
        )

    def test_stopped_provider_rejects_new_session(self):
        plan = plan_for(self.controller)
        self.controller.stop_event.set()
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                self.controller.begin_session(plan, assertion_for(self.controller, plan))

    def test_warm_provider_refreshes_measured_joint_state_without_a_session(self):
        config = provider_config()
        controller = ContactController(
            config,
            self.basic,
            provider_instance_id="warm-instance",
            provider_boot_id="warm-boot",
        )
        controller.start()
        self.basic.state_value["positions_rad"][2] = 0.314
        deadline = time.monotonic() + 1.0
        while (
            controller.snapshot()["positions_rad"][2] != 0.314
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        try:
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["positions_rad"][2], 0.314)
            self.assertFalse(snapshot["ready"])
            self.assertTrue(snapshot["joint_state_valid"])
        finally:
            controller.stop()

    def test_inactivity_watchdog_relaxes_without_arrival(self):
        config = provider_config()
        controller = ContactController(
            config,
            self.basic,
            provider_instance_id="watchdog-instance",
            provider_boot_id="watchdog-boot",
        )
        controller.start()
        controller.enter_hot()
        plan = plan_for(controller)
        plan["execution_id"] = "watchdog-execution"
        plan["manager_authority"]["owner_id"] = "watchdog-execution"
        plan["steps"][0]["next_command_timeout_s"] = 0.10
        current_q = np.asarray(self.basic.state_value["positions_rad"][:6])
        current_pose = controller.kinematics.evaluate(current_q).controlled_transform
        plan["steps"][0]["locked_joint_names"] = []
        plan["steps"][0]["target"]["position_m"] = current_pose[:3, 3].tolist()
        plan["steps"][0]["target"]["orientation_xyzw"] = quaternion_from_matrix(
            current_pose[:3, :3]
        )
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            controller.begin_session(plan, assertion_for(controller, plan))
            controller.move("watchdog-execution", 0)
            deadline = time.monotonic() + 1.0
            while controller.session is not None and time.monotonic() < deadline:
                time.sleep(0.02)
        try:
            self.assertIsNone(controller.session)
            self.assertEqual(controller.last_disposition, "WATCHDOG_RELAXED")
            self.assertIsNone(self.basic.lease)
        finally:
            controller.stop()

    def test_watchdog_begins_after_velocity_limited_transition(self):
        plan = plan_for(self.controller)
        plan["steps"][0]["next_command_timeout_s"] = 0.25
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            self.controller.begin_session(
                plan,
                assertion_for(self.controller, plan),
            )
            result = self.controller.move("execution-1", 0)
        remaining = self.controller.session.deadline_monotonic - time.monotonic()
        self.assertAlmostEqual(
            remaining,
            result["velocity_limited_transition_time_s"] + 0.25,
            delta=0.05,
        )
        self.assertEqual(
            result["next_command_watchdog_after_transition_s"],
            0.25,
        )

    def test_warm_provider_rejects_contact_session_until_hot(self):
        controller = ContactController(
            provider_config(),
            self.basic,
            provider_instance_id="warm-reject-instance",
            provider_boot_id="warm-reject-boot",
        )
        controller._refresh_runtime_binding()
        plan = plan_for(controller)
        with mock.patch.dict(
            "os.environ", {"MIDBRAIN_CONTACT_SLICING_SECRET": SECRET}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "must be HOT"):
                controller.begin_session(plan, assertion_for(controller, plan))


if __name__ == "__main__":
    unittest.main()
