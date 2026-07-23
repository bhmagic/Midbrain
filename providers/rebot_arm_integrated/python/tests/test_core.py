from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys
from unittest.mock import patch

import numpy as np

from rebot_arm_integrated.basic_client import BasicLease
from rebot_arm_integrated.command_semantics import LatchedEndpointCommand
from rebot_arm_integrated.config_repair import ensure_controller_config, validate_controller_config
from rebot_arm_integrated.contact import TorqueBaseline
from rebot_arm_integrated.controller import (
    CONTROL_MODE,
    IK_POSE_6DOF,
    IK_POSITION_3DOF,
    INTERACTION_HOLD_LB,
    INTERACTION_ONE_SHOT,
    MODE_MIT,
    PlanningRejected,
    IntegratedController,
)
from rebot_arm_integrated.kinematics import ArmKinematics, rpy_matrix, transform
from rebot_arm_integrated.http_client import HttpStatusError
from rebot_arm_integrated.modes import CONTACT_WORK, PRESS_MIT, TRANSIT_SPEED
from rebot_arm_integrated.service import IntegratedService


INTEGRATED_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = INTEGRATED_ROOT.parent
BASIC_ROOT = PACKAGE_ROOT / "rebot_arm_dm"
sys.path.insert(0, str(BASIC_ROOT / "python"))


def load_config() -> dict:
    return json.loads((INTEGRATED_ROOT / "config_templates" / "controller.default.json").read_text())


def load_public_model() -> dict:
    model_path = BASIC_ROOT / "config" / "arm_model.json"
    calibration_path = BASIC_ROOT / "config" / "arm_calibration.json"
    if not model_path.exists():
        model_path = BASIC_ROOT / "config_templates" / "arm_model.factory.json"
    if not calibration_path.exists():
        calibration_path = (
            BASIC_ROOT
            / "config_templates"
            / "arm_calibration.initial.json"
        )
    model = json.loads(model_path.read_text())
    calibration = json.loads(calibration_path.read_text())
    by_name = {item["name"]: item for item in calibration["joints"]}
    for joint in model["joints"]:
        joint["calibrated"] = copy.deepcopy(by_name[joint["name"]])
    return model


class FakeBasic:
    def __init__(self):
        self._model = load_public_model()
        self._state = {
            "provider_state": "SAFE_HOLD_GRAVITY_FLOAT",
            "health": "HEALTHY",
            "feedback_age_ms": 0.0,
            "positions_rad": [0.0, -0.18, -0.22, 0.12, 0.02, -0.05, -0.35],
            "velocities_rad_s": [0.0] * 7,
            "torques_nm": [0.15, -2.0, -6.0, -1.2, 0.08, -0.04, 0.0],
            "active_command_modes": [MODE_MIT] * 7,
            "float_transition_pending_joint_indices": [],
            "mode_transition": {"active": False},
            "last_error": None,
            "payload": {"mass_kg": 0.0, "com_tool_m": [0.0, 0.0, 0.0]},
            "gravity_compensation": {
                "total_nm": [0.1, -1.9, -5.8, -1.1, 0.05, -0.03, 0.0],
                "payload_nm": [0.0] * 7,
                "clamped_to_motor_tmax": [False] * 7,
            },
        }
        self.lease: BasicLease | None = None
        self.commands: list[list[dict]] = []
        self.float_count = 0
        self.release_count = 0
        self.generation = 0
        self.safe_home_stop_count = 0
        self.payload_updates: list[tuple[float, list[float]]] = []
        self.reject_next_command = False

    def model(self):
        return copy.deepcopy(self._model)

    def state(self):
        return copy.deepcopy(self._state)

    def health(self):
        return {"status": "ok"}

    def acquire(self, holder, duration_ms):
        self.generation += 1
        self.lease = BasicLease("lease", self.generation, time.monotonic() + duration_ms / 1000.0, holder)
        return copy.deepcopy(self.lease)

    def renew(self, duration_ms):
        if self.lease is None:
            raise RuntimeError("no lease")
        self.lease.expires_monotonic = time.monotonic() + duration_ms / 1000.0
        return copy.deepcopy(self.lease)

    def lease_snapshot(self):
        return None if self.lease is None else copy.deepcopy(self.lease)

    def clear_lease(self, *args, **kwargs):
        self.lease = None

    def command(self, commands, timeout_ms=250):
        if self.reject_next_command:
            self.reject_next_command = False
            raise HttpStatusError(
                400,
                "http://127.0.0.1:8791/v1/control/command",
                '{"error":"test validation rejection"}',
            )
        self.commands.append(copy.deepcopy(commands))
        for command in commands:
            self._state["active_command_modes"][int(command["joint_index"])] = str(command["mode"])
        return {"accepted": True}

    def float(self, reason=""):
        self.float_count += 1
        self._state["provider_state"] = "SAFE_HOLD_GRAVITY_FLOAT"
        self._state["active_command_modes"] = [MODE_MIT] * 7
        self._state["float_transition_pending_joint_indices"] = []
        self._state["mode_transition"] = {"active": False}
        return {"status": "gravity_float"}

    def set_payload(self, mass_kg, com_tool_m):
        value = (float(mass_kg), [float(v) for v in com_tool_m])
        self.payload_updates.append(value)
        self._state["payload"] = {"mass_kg": value[0], "com_tool_m": value[1]}
        return {"status": "payload_updated", "payload": copy.deepcopy(self._state["payload"])}

    def release(self, reason=""):
        self.release_count += 1
        self.lease = None

    def safe_home_stop(self):
        self.safe_home_stop_count += 1
        return {"status": "safe_home_then_stop"}


def prepared_controller(*, short_trajectory: bool = False) -> tuple[IntegratedController, FakeBasic]:
    config = load_config()
    if short_trajectory:
        config["runtime"]["duration_s"] = 0.25
        config["trajectory"]["send_rate_hz"] = 100.0
    basic = FakeBasic()
    controller = IntegratedController(config, basic)
    controller.enter_hot()
    controller.update_platform_status(True, True, {}, motion_inhibited=False)
    return controller, basic


def wait_until(predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid_and_uses_twenty_cm_envelope(self):
        config = load_config()
        validate_controller_config(config)
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(config["trajectory"]["maximum_translation_per_commit_m"], 0.20)
        self.assertEqual(config["contact"]["maximum_translation_m"], 0.20)
        self.assertEqual(config["contact"]["budget_mode"], "JOINT_6")
        self.assertEqual(
            config["contact"]["task_torque_budget_nm"],
            [2.0, 2.0, 2.0, 1.0, 1.0, 1.0],
        )
        self.assertEqual(config["runtime"]["interaction_mode"], INTERACTION_ONE_SHOT)
        self.assertEqual(config["runtime"]["ik_mode"], IK_POSITION_3DOF)
        self.assertEqual(config["gripper"]["mode"], "MIT")
        self.assertAlmostEqual(config["gripper"]["open_position_rad"], -4.886921905584122)
        self.assertAlmostEqual(config["gripper"]["closed_position_rad"], -0.3490658503988659)

    def test_config_repair_replaces_old_schema_but_preserves_connection_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config_templates").mkdir()
            (root / "config_templates" / "controller.default.json").write_text(json.dumps(load_config()))
            active = root / "config" / "controller.json"
            active.parent.mkdir()
            active.write_text(json.dumps({"schema": "old", "listen_port": 9999, "basic_controller_url": "http://x"}))
            result = ensure_controller_config(root, active)
            self.assertTrue(result.repaired)
            self.assertEqual(result.config["listen_port"], 9999)
            self.assertEqual(result.config["basic_controller_url"], "http://x")
            self.assertEqual(result.config["schema_version"], 3)


class SafeTerminationTests(unittest.TestCase):
    def test_windows_detached_shutdown_requires_launch_id_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            provider_root = Path(temp) / "providers" / "rebot_arm_integrated"
            scripts = provider_root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "safe_terminate_detached.ps1").write_text(
                "param([string]$LaunchId)\n",
                encoding="utf-8",
            )
            service = IntegratedService(object(), load_config(), None, None)
            service.provider_root = provider_root

            def acknowledged_process(arguments, **kwargs):
                launch_id = arguments[arguments.index("-LaunchId") + 1]
                log_path = provider_root / "runtime_logs" / "safe_terminate.log"
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(
                        "Authoritative safe termination started. "
                        f"launch_id={launch_id}\n"
                    )

                class Process:
                    pid = 12345

                    @staticmethod
                    def poll():
                        return None

                return Process()

            with patch(
                "rebot_arm_integrated.service.subprocess.Popen",
                side_effect=acknowledged_process,
            ):
                result = service.start_safe_termination()

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["safe_termination"]["state"], "RUNNING")
            self.assertEqual(result["safe_termination"]["process_id"], 12345)


class ControllerTests(unittest.TestCase):
    def test_gripper_rb_open_release_latches_mit_endpoint_without_float(self):
        controller, basic = prepared_controller()
        controller.set_engaged(True)
        before_float = basic.float_count
        controller.update_input({"gripper_open": True})
        controller._tick()
        command = basic.commands[-1]
        self.assertEqual(len(command), 1)
        self.assertEqual(command[0]["joint_index"], 6)
        self.assertEqual(command[0]["mode"], MODE_MIT)
        self.assertAlmostEqual(command[0]["values"]["position_rad"], controller.gripper_open_position_rad)
        self.assertEqual(controller.snapshot()["gripper"]["active_action"], "OPEN")
        controller.update_input({"gripper_open": False})
        controller.gripper_last_send_monotonic = 0.0
        controller._tick()
        snapshot = controller.snapshot()
        self.assertEqual(basic.float_count, before_float)
        self.assertEqual(snapshot["gripper"]["active_action"], "OPEN")
        self.assertTrue(snapshot["gripper"]["latched_hold"])
        self.assertEqual(snapshot["gripper"]["release_behavior"], "LATCH_LAST_ENDPOINT")
        command = basic.commands[-1]
        self.assertEqual(len(command), 1)
        self.assertEqual(command[0]["joint_index"], 6)
        self.assertEqual(command[0]["mode"], MODE_MIT)

    def test_latched_pos_tor_gripper_is_appended_to_arm_command_envelope(self):
        controller, _ = prepared_controller()
        controller.set_gripper_settings({"mode": "POS_TOR"})
        controller.set_engaged(True)
        controller.update_input({"gripper_close": True})
        controller._tick()
        controller.update_input({"gripper_close": False})
        with controller.lock:
            commands = controller._build_commands_locked(
                np.asarray(controller.commanded_q, dtype=float),
                np.zeros(6, dtype=float),
            )
        self.assertEqual(len(commands), 7)
        self.assertEqual(commands[-1]["joint_index"], 6)
        self.assertEqual(commands[-1]["mode"], "POSITION_EFFORT_LIMITED")
        self.assertAlmostEqual(
            commands[-1]["values"]["position_rad"],
            controller.gripper_closed_position_rad,
        )

    def test_gripper_rt_close_can_select_pos_tor(self):
        controller, basic = prepared_controller()
        controller.set_gripper_settings({"mode": "POS_TOR"})
        controller.set_engaged(True)
        controller.update_input({"gripper_close": True})
        controller._tick()
        command = basic.commands[-1][0]
        self.assertEqual(command["joint_index"], 6)
        self.assertEqual(command["mode"], "POSITION_EFFORT_LIMITED")
        self.assertAlmostEqual(command["values"]["position_rad"], controller.gripper_closed_position_rad)
        self.assertAlmostEqual(command["values"]["torque_limit_ratio"], 0.15)

    def test_gripper_requires_engage_and_does_not_send(self):
        controller, basic = prepared_controller()
        controller.update_input({"gripper_open": True})
        controller._tick()
        self.assertFalse(basic.commands)
        self.assertIn("Engage", controller.snapshot()["gripper"]["last_error"])

    def test_warm_refuses_to_release_lease_until_float_mode_transitions_finish(self):
        controller, basic = prepared_controller()
        controller.config["safety"]["float_verify_timeout_ms"] = 30

        def incomplete_float(reason=""):
            basic.float_count += 1
            basic._state["provider_state"] = "SAFE_HOLD_GRAVITY_FLOAT"
            basic._state["float_transition_pending_joint_indices"] = [0]
            return {"status": "gravity_float"}

        basic.float = incomplete_float
        with self.assertRaisesRegex(RuntimeError, "refused to release"):
            controller.enter_warm()
        self.assertIsNotNone(basic.lease_snapshot())

    def test_press_transit_and_contact_are_physically_enabled_backends(self):
        controller, _ = prepared_controller()
        state = controller.snapshot()
        self.assertEqual(state["control_mode"], CONTROL_MODE)
        self.assertEqual(state["execution_mode"], PRESS_MIT)
        self.assertEqual(state["basic_execution_mode"], MODE_MIT)
        self.assertEqual(
            state["safety"]["physically_enabled_execution_modes"],
            [PRESS_MIT, TRANSIT_SPEED, CONTACT_WORK],
        )

    def test_manager_discovery_advertises_only_reviewed_arm_motion_profiles(self):
        controller, _ = prepared_controller()
        state = controller.snapshot()
        readiness = state["capability_readiness"]
        self.assertTrue(readiness["robot.motion.arm.integrated.mit.one_shot"])
        self.assertTrue(readiness["robot.motion.arm.integrated.mit.continuous"])
        self.assertTrue(
            readiness["robot.motion.arm.integrated.pos_vel.one_shot_limited"]
        )
        self.assertFalse(
            any(
                "pos_vel.continuous" in capability
                or "pos_tor.one_shot" in capability
                or "contact_work" in capability
                for capability in readiness
            )
        )
        self.assertEqual(
            state["capability_profiles"][
                "robot.motion.arm.integrated.pos_vel.one_shot_limited"
            ]["constraints"],
            {
                "maximum_path_length_m": 0.2,
                "load": "NO_PAYLOAD_OR_HIGH_EXTERNAL_LOAD",
                "stability_beyond_constraints": "NOT_ESTABLISHED",
            },
        )
        self.assertFalse(
            state["non_discoverable_experiments"]["TRANSIT_SPEED_HOLD_LB"][
                "manager_capability_advertised"
            ]
        )
        self.assertFalse(
            state["non_discoverable_experiments"][
                "CONTACT_WORK_ONE_SHOT_POS_TOR"
            ]["manager_capability_advertised"]
        )

    def test_provider_capability_catalog_maps_upstream_operations(self):
        controller, _ = prepared_controller()
        service = IntegratedService(controller, load_config(), None, None)
        catalog = service.capability_catalog()
        names = {item["capability"] for item in catalog["capabilities"]}
        self.assertIn("robot.motion.arm.integrated.mit.one_shot", names)
        self.assertIn(
            "robot.motion.arm.integrated.pos_vel.one_shot_limited",
            names,
        )
        self.assertEqual(
            catalog["upstream_operations"]["cartesian_target_staging"][
                "transport"
            ],
            "FABRIC",
        )
        self.assertEqual(
            catalog["upstream_operations"]["engage"]["caller_policy"],
            "OPERATOR_OR_OPERATOR_SUPERVISED_SKILL",
        )
        self.assertEqual(
            catalog["upstream_operations"]["teleop_input"]["path"],
            "/v1/teleop",
        )
        self.assertFalse(
            catalog["physical_execution_gate"]["upstream_motion_authority"]
        )

    def test_preview_solves_without_sending_a_motor_command(self):
        controller, basic = prepared_controller()
        controller.set_runtime_settings({"execution_mode": TRANSIT_SPEED})
        controller.staged_target[0, 3] += 0.002
        result = controller.preview_staged_target()
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(basic.commands, [])

    def test_physical_ik_keeps_large_position_residual_as_telemetry(self):
        controller, basic = prepared_controller()
        controller.set_runtime_settings(
            {
                "execution_mode": CONTACT_WORK,
                "ik_mode": IK_POSE_6DOF,
            }
        )
        q = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        result = type(
            "IkResult",
            (),
            {
                "q_goal": q.copy(),
                "position_residual_m": 0.2098,
                "orientation_residual_rad": 0.8,
                "iterations": 180,
                "sigma_min": 0.001,
            },
        )()
        with patch.object(
            controller.kinematics,
            "solve_weighted_pose",
            return_value=result,
        ):
            accepted = controller._solve_target_locked(
                q,
                controller.staged_target.copy(),
            )
        self.assertAlmostEqual(accepted.position_residual_m, 0.2098)

    def test_contact_work_builds_latched_pos_tor_endpoint_from_explicit_budget(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings(
            {
                "execution_mode": CONTACT_WORK,
                "interaction_mode": INTERACTION_ONE_SHOT,
                "ik_mode": IK_POSE_6DOF,
                "duration_s": 0.6,
                "contact_torque_budget_nm": [0.1, 0.1, 0.1, 0.05, 0.05, 0.05],
            }
        )
        controller.torque_baseline = TorqueBaseline(
            np.asarray(basic._state["positions_rad"][:6], dtype=float),
            np.asarray(basic._state["torques_nm"][:6], dtype=float),
            np.asarray(
                basic._state["gravity_compensation"]["total_nm"][:6], dtype=float
            ),
            np.asarray([0.01] * 6, dtype=float),
            15,
        )
        controller.baseline_capture_state = "CAPTURED"
        controller.set_engaged(True)
        floats_before_commit = basic.float_count
        with controller.lock:
            controller.staged_target[0, 3] += 0.002
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(
            wait_until(
                lambda: any(
                    frame and frame[0]["mode"] == "POSITION_EFFORT_LIMITED"
                    for frame in basic.commands
                )
            )
        )
        command_seen_at = time.monotonic()
        command = next(
            frame
            for frame in basic.commands
            if frame and frame[0]["mode"] == "POSITION_EFFORT_LIMITED"
        )
        self.assertEqual(len(command), 6)
        self.assertTrue(
            all(0.0 < item["values"]["torque_limit_ratio"] <= 0.25 for item in command)
        )
        self.assertIsNotNone(controller.contact_torque_limit_ratios)
        self.assertEqual(basic.float_count, floats_before_commit)
        self.assertEqual(
            controller.baseline_capture_state,
            "CAPTURED",
        )
        self.assertTrue(
            wait_until(lambda: controller.trajectory is None, timeout=2.0)
        )
        self.assertGreaterEqual(time.monotonic() - command_seen_at, 0.5)
        self.assertEqual(
            controller.last_completed_trajectory["execution_mode"],
            CONTACT_WORK,
        )
        self.assertTrue(controller.last_completed_trajectory["float_confirmed"])

    def test_contact_work_accepts_isotropic_wrench_budget_and_forces_one_shot(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings(
            {
                "execution_mode": CONTACT_WORK,
                "interaction_mode": INTERACTION_HOLD_LB,
                "ik_mode": IK_POSE_6DOF,
                "contact_budget_mode": "ISOTROPIC_2",
                "contact_isotropic_force_budget_n": 0.5,
                "contact_isotropic_torque_budget_nm": 0.05,
            }
        )
        self.assertEqual(controller.interaction_mode, INTERACTION_ONE_SHOT)
        q_reference = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        with controller.lock:
            isotropic_budget = controller._contact_joint_budget_locked(q_reference)
        self.assertEqual(isotropic_budget.shape, (6,))
        self.assertTrue(np.all(isotropic_budget > 0.0))
        controller.torque_baseline = TorqueBaseline(
            np.asarray(basic._state["positions_rad"][:6], dtype=float),
            np.asarray(basic._state["torques_nm"][:6], dtype=float),
            np.asarray(
                basic._state["gravity_compensation"]["total_nm"][:6], dtype=float
            ),
            np.asarray([0.01] * 6, dtype=float),
            15,
        )
        controller.baseline_capture_state = "CAPTURED"
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.002
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(
            wait_until(
                lambda: controller.latched_endpoint is not None
                and controller.latched_endpoint.basic_mode == "POSITION_EFFORT_LIMITED"
            )
        )
        first_endpoint = controller.latched_endpoint
        with controller.lock:
            controller.staged_target[1, 3] += 0.001
        controller._replan_continuous()
        self.assertIs(controller.latched_endpoint, first_endpoint)
        self.assertEqual(controller.live_replan_count, 0)
        self.assertEqual(controller.config["contact"]["maximum_translation_m"], 0.20)
        self.assertIsNotNone(controller.contact_effective_joint_budget_nm)
        controller.request_float()

    def test_transit_one_shot_returns_to_float_after_confirmed_arrival(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"execution_mode": TRANSIT_SPEED, "interaction_mode": INTERACTION_ONE_SHOT})
        controller.config["trajectory"]["arrival_stable_samples"] = 2
        controller.set_engaged(True)
        floats_before_commit = basic.float_count
        with controller.lock:
            controller.staged_target[0, 3] += 0.01
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: any(frame and frame[0]["mode"] == "POSITION_VELOCITY_LIMITED" for frame in basic.commands)))
        goal = controller.goal_q.copy()
        basic._state["positions_rad"][:6] = goal.tolist()
        basic._state["velocities_rad_s"][:6] = [0.0] * 6
        self.assertTrue(wait_until(lambda: controller.trajectory is None, timeout=2.0))
        modes = {item["mode"] for frame in basic.commands for item in frame}
        self.assertIn("POSITION_VELOCITY_LIMITED", modes)
        self.assertNotIn(MODE_MIT, modes)
        self.assertGreater(basic.float_count, floats_before_commit)

    def test_transit_basic_400_keeps_last_accepted_endpoint_without_float(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings(
            {"execution_mode": TRANSIT_SPEED, "interaction_mode": INTERACTION_ONE_SHOT}
        )
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.006
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: len(basic.commands) >= 1))
        floats_before_rejection = basic.float_count
        rejected_before = controller.rejected_count
        basic.reject_next_command = True
        self.assertTrue(
            wait_until(lambda: controller.rejected_count > rejected_before, timeout=1.0)
        )
        self.assertIsNotNone(controller.trajectory)
        self.assertEqual(basic.float_count, floats_before_rejection)
        self.assertIn("keeping the last accepted endpoint", controller.last_error)
        controller.request_float()

    def test_transit_hold_lb_release_is_explicit_float_boundary(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings(
            {"execution_mode": TRANSIT_SPEED, "interaction_mode": INTERACTION_HOLD_LB}
        )
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[1, 3] += 0.005
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(
            wait_until(
                lambda: any(
                    frame and frame[0]["mode"] == "POSITION_VELOCITY_LIMITED"
                    for frame in basic.commands
                )
            )
        )
        floats_before_release = basic.float_count
        controller.update_input({"lb": False})
        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        self.assertGreater(basic.float_count, floats_before_release)

    def test_operator_baseline_capture_stays_in_float_and_sends_no_target(self):
        controller, basic = prepared_controller()
        controller.config["contact"]["baseline_duration_s"] = 0.5
        controller.config["contact"]["baseline_minimum_samples"] = 5
        result = controller.capture_contact_baseline()
        self.assertTrue(result["captured"])
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(controller.baseline_capture_state, "CAPTURED")
        self.assertGreater(basic.float_count, 0)
        self.assertEqual(basic.commands, [])

    def test_contact_monitor_saturates_joint_without_raising_or_dropping_endpoint(self):
        controller, basic = prepared_controller()
        controller.set_runtime_settings(
            {"contact_torque_budget_nm": [0.1] * 6}
        )
        controller.torque_baseline = TorqueBaseline(
            np.asarray(basic._state["positions_rad"][:6], dtype=float),
            np.asarray(basic._state["torques_nm"][:6], dtype=float),
            np.asarray(
                basic._state["gravity_compensation"]["total_nm"][:6], dtype=float
            ),
            np.asarray([0.01] * 6, dtype=float),
            15,
        )
        changed = basic.state()
        changed["torques_nm"][2] += 0.2
        controller.latched_endpoint = LatchedEndpointCommand.create(
            "POSITION_EFFORT_LIMITED",
            [0.0] * 6,
            [0.0] * 6,
            [0.1] * 6,
            keepalive_period_s=0.1,
            torque_limit_ratios=[0.05] * 6,
        )
        controller._update_contact_monitor_locked(changed)
        self.assertEqual(controller.contact_limit_violations, [2])
        self.assertEqual(controller.contact_saturated_joint_indices, [2])
        self.assertEqual(
            controller.latched_endpoint.torque_limit_ratios[2],
            controller.basic_model["control"]["physical_test_pos_tor_ratio_cap"][2],
        )

    def test_target_editing_is_nonphysical(self):
        controller, basic = prepared_controller()
        before = controller.staged_target.copy()
        controller.update_input({"x": 1.0, "lb": False})
        with controller.lock:
            controller.last_target_update = time.monotonic() - 0.05
        controller._tick()
        self.assertGreater(controller.staged_target[0, 3], before[0, 3])
        self.assertFalse(basic.commands)

    def test_three_dof_ignores_orientation_edit_input(self):
        controller, _ = prepared_controller()
        before = controller.staged_target[:3, :3].copy()
        controller.update_input({"yaw": 1.0, "pitch": 1.0, "roll": 1.0})
        with controller.lock:
            controller.last_target_update = time.monotonic() - 0.05
        controller._tick()
        self.assertTrue(np.allclose(before, controller.staged_target[:3, :3]))

    def test_six_dof_enables_orientation_edit_input(self):
        controller, _ = prepared_controller()
        controller.set_runtime_settings({"ik_mode": IK_POSE_6DOF})
        before = controller.staged_target[:3, :3].copy()
        controller.update_input({"yaw": 1.0})
        with controller.lock:
            controller.last_target_update = time.monotonic() - 0.05
        controller._tick()
        self.assertFalse(np.allclose(before, controller.staged_target[:3, :3]))

    def test_one_shot_lb_rising_edge_moves_once_and_returns_to_float(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.0015
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        first = controller.commit_count
        controller.update_input({"lb": True})
        controller._tick()
        self.assertEqual(controller.commit_count, first)
        controller.update_input({"lb": False})
        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        self.assertGreater(len(basic.commands), 3)
        self.assertGreater(basic.float_count, 0)

    def test_rejected_optional_preview_does_not_veto_operator_commit(self):
        controller, _ = prepared_controller(short_trajectory=True)
        controller.set_engaged(True)
        with controller.lock:
            origin = controller.kinematics.controlled_frame(controller._measured_positions_locked()[:6], controller._tool_to_control_locked())
            controller.staged_target = origin.copy()
            controller.staged_target[0, 3] += 0.40
        preview = controller.preview_staged_target()
        self.assertTrue(preview["target_clamped"])
        self.assertFalse(preview["planning_valid"])
        self.assertTrue(any("joint travel" in reason for reason in preview["planning_reasons"]))
        controller.update_input({"lb": True})
        controller._tick()
        self.assertIsNotNone(controller.last_committed_target)

    def test_all_motion_commands_are_impedance(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[1, 3] += 0.005
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        self.assertTrue(basic.commands)
        self.assertEqual({item["mode"] for frame in basic.commands for item in frame}, {MODE_MIT})

    def test_kp_multiplier_exposes_protocol_clamping(self):
        controller, _ = prepared_controller()
        controller.set_runtime_settings({"kp_multiplier": 10.0})
        gains = controller.snapshot()["runtime"]["effective_gains"]
        self.assertEqual([gain["effective_kp"] for gain in gains[:3]], [500.0, 500.0, 500.0])
        self.assertTrue(all(gain["kp_clamped"] for gain in gains[:3]))
        self.assertEqual([gain["effective_kp"] for gain in gains[3:]], [180.0, 180.0, 180.0])
        self.assertTrue(all(gain["kd_clamped"] for gain in gains))

    def test_payload_settings_are_forwarded_under_basic_lease(self):
        controller, basic = prepared_controller()
        controller.set_runtime_settings({"payload_mass_kg": 1.2, "payload_com_tool_m": [0.0, 0.0, 0.12]})
        self.assertEqual(basic.payload_updates[-1], (1.2, [0.0, 0.0, 0.12]))
        state = controller.snapshot()
        self.assertEqual(state["runtime"]["payload_mass_kg"], 1.2)

    def test_controlled_frame_offset_recaptures_target(self):
        controller, _ = prepared_controller()
        before = controller.staged_target[:3, 3].copy()
        controller.set_runtime_settings({"controlled_frame_offset_xyz_m": [0.0, 0.0, 0.05]})
        after = controller.staged_target[:3, 3].copy()
        self.assertGreater(float(np.linalg.norm(after - before)), 0.01)

    def test_hold_lb_replans_and_release_floats(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"interaction_mode": INTERACTION_HOLD_LB, "replan_interval_s": 0.05})
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.002
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: len(basic.commands) >= 2))
        with controller.lock:
            controller.staged_target[1, 3] += 0.001
        controller._replan_continuous()
        self.assertGreaterEqual(controller.live_replan_count, 1)
        self.assertIsNotNone(controller.trajectory)
        controller.update_input({"lb": False})
        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        self.assertGreater(basic.float_count, 0)

    def test_runtime_settings_block_during_motion(self):
        controller, _ = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"interaction_mode": INTERACTION_HOLD_LB})
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.002
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: controller.trajectory is not None))
        with self.assertRaises(RuntimeError):
            controller.set_runtime_settings({"kp_multiplier": 2.0})
        controller.update_input({"lb": False})

    def test_rejected_hold_replan_keeps_last_valid_plan_without_forcing_float(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"interaction_mode": INTERACTION_HOLD_LB})
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[0, 3] += 0.002
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: controller.trajectory is not None))
        active_plan = controller.trajectory
        float_count = basic.float_count
        with patch.object(controller, "_solve_target_locked", side_effect=PlanningRejected("test residual")):
            controller._replan_continuous()
        self.assertIs(controller.trajectory, active_plan)
        self.assertEqual(basic.float_count, float_count)
        self.assertIn("keeping last valid plan", controller.last_error)
        controller.update_input({"lb": False})


class FabricInputTests(unittest.TestCase):
    def test_external_cartesian_command_stages_target_without_motion(self):
        controller, basic = prepared_controller()
        current = controller.staged_target[:3, 3].copy()
        result = controller.stage_external_command(
            {
                "command_type": "CARTESIAN_TARGET",
                "target": {"position_m": (current + np.array([0.01, 0.0, 0.0])).tolist()},
                "settings": {"kp_multiplier": 2.0},
            },
            source="fabric:test",
            metadata={"sequence": 7},
        )
        self.assertTrue(result["accepted"])
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(controller.kp_multiplier, 2.0)
        self.assertAlmostEqual(controller.staged_target[0, 3], current[0] + 0.01)
        self.assertFalse(basic.commands)
        state = controller.snapshot()
        self.assertEqual(state["external_input"]["update_count"], 1)
        self.assertEqual(state["external_input"]["last_metadata"]["sequence"], 7)

    def test_upstream_ik_location_offset_and_gravity_offset_are_distinct(self):
        controller, basic = prepared_controller()
        location = controller.staged_target[:3, 3].copy()
        result = controller.stage_external_command(
            {
                "ik_location": {
                    "position_m": location.tolist(),
                    "rpy_rad": [0.0, 0.0, 0.0],
                },
                "ik_offset": {
                    "xyz_m": [0.0, 0.0, 0.08],
                    "rpy_rad": [0.0, 0.0, 0.0],
                },
                "ik_gravity_offset": {
                    "xyz_m": [0.0, 0.0, 0.006],
                    "rpy_rad": [0.0, 0.0, 0.0],
                },
            },
            source="fabric:test",
        )
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(controller.tool_offset_xyz_m[2], 0.08)
        self.assertAlmostEqual(controller.staged_target[2, 3], location[2] + 0.006)
        components = controller.snapshot()["external_input"]["last_metadata"][
            "ik_components"
        ]
        self.assertEqual(components["location"]["position_m"], location.tolist())
        self.assertEqual(
            components["tool_to_acting_point_offset"]["xyz_m"],
            [0.0, 0.0, 0.08],
        )
        self.assertEqual(
            components["base_frame_gravity_offset"]["xyz_m"],
            [0.0, 0.0, 0.006],
        )
        self.assertFalse(basic.commands)

    def test_fabric_latest_observation_is_consumed_once_and_stale_is_ignored(self):
        controller, _ = prepared_controller()
        config = load_config()
        service = IntegratedService(controller, config, None, "http://fabric")
        position = controller.staged_target[:3, 3].copy()
        observation = {
            "schema": config["fabric_input"]["schema"],
            "provider_id": "skill.test",
            "provider_instance_id": "skill-instance",
            "boot_id": "skill-boot",
            "sequence": 11,
            "observed_at_us": time.time_ns() // 1000,
            "freshness_ms": 650,
            "valid": True,
            "data": {
                "command_type": "CARTESIAN_TARGET",
                "target": {"position_m": (position + np.array([0.0, 0.01, 0.0])).tolist()},
            },
        }
        service.platform.latest = lambda stream: copy.deepcopy(observation)
        service._consume_fabric_input()
        self.assertEqual(service.fabric_input_status["last_result"], "ACCEPTED")
        self.assertEqual(service.fabric_input_status["accepted_count"], 1)
        service._consume_fabric_input()
        self.assertEqual(service.fabric_input_status["last_result"], "DUPLICATE")
        self.assertEqual(service.fabric_input_status["accepted_count"], 1)

        stale = copy.deepcopy(observation)
        stale["sequence"] = 12
        stale["observed_at_us"] = (time.time_ns() // 1000) - 2_000_000
        service.platform.latest = lambda stream: copy.deepcopy(stale)
        service._consume_fabric_input()
        self.assertEqual(service.fabric_input_status["last_result"], "STALE_IGNORED")
        self.assertEqual(service.fabric_input_status["stale_count"], 1)

    def test_fabric_semantic_scene_is_staged_without_motion_authority(self):
        controller, basic = prepared_controller()
        config = load_config()
        service = IntegratedService(controller, config, None, "http://fabric")
        observation = {
            "schema": config["scene_input"]["schema"],
            "provider_id": "scene.test",
            "provider_instance_id": "scene-instance",
            "boot_id": "scene-boot",
            "sequence": 4,
            "observed_at_us": time.time_ns() // 1000,
            "freshness_ms": 1000,
            "valid": True,
            "data": {
                "scene_revision": "scene-4",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {"sphere_id": "knife-1", "object_id": "knife", "center_m": [0.4, 0.0, 0.2], "radius_m": 0.03, "type": "KEEP_OUT"}
                ],
            },
        }
        service.platform.latest = lambda stream: copy.deepcopy(observation)
        service._consume_scene_input()
        self.assertEqual(service.scene_input_status["last_result"], "ACCEPTED")
        self.assertFalse(service.scene_input_status["physical_motion_authorized"])
        self.assertEqual(controller.snapshot()["planning"]["scene"]["revision"], "scene-4")
        self.assertEqual(basic.commands, [])


class KinematicsTests(unittest.TestCase):
    def test_controlled_frame_offset_moves_the_acting_point(self):
        kin = ArmKinematics(load_public_model())
        q = np.array([0.0, -0.18, -0.22, 0.12, 0.02, -0.05])
        tool = kin.controlled_frame(q)
        controlled = kin.controlled_frame(q, transform([0.0, 0.0, 0.10]))
        self.assertAlmostEqual(float(np.linalg.norm(controlled[:3, 3] - tool[:3, 3])), 0.10, places=6)

    def test_six_dof_solver_recovers_reachable_pose(self):
        kin = ArmKinematics(load_public_model())
        seed = np.array([0.0, -0.3, -0.35, 0.08, 0.03, -0.04])
        known = seed + np.array([0.015, -0.01, 0.012, 0.008, -0.006, 0.01])
        target = kin.controlled_frame(known, transform([0.0, 0.0, 0.04], rpy_matrix([0.0, 0.05, 0.0])))
        result = kin.solve_weighted_pose(
            seed,
            target,
            position_tolerance_m=0.0015,
            orientation_tolerance_rad=0.035,
            maximum_iterations=220,
            damping=0.025,
            maximum_step_rad=0.035,
            joint_margin_rad=0.03,
            orientation_weight_m_per_rad=0.10,
            orientation_required=True,
            tool_to_control=transform([0.0, 0.0, 0.04], rpy_matrix([0.0, 0.05, 0.0])),
        )
        self.assertLess(result.position_residual_m, 0.005)
        self.assertLess(result.orientation_residual_rad, 0.10)


if __name__ == "__main__":
    unittest.main()
