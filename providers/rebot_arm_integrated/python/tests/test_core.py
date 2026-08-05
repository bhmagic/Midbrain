from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import sys
from typing import Any
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
    IDLE_COMPLIANT_HOLD,
    IDLE_POSITION_LOCK,
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
        config["trajectory"]["one_shot_arrival_settle_timeout_s"] = 0.05
        # Unit tests do not run the controller's Basic state polling thread.
        config["max_basic_state_age_ms"] = 1000
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
    def test_default_config_is_valid_and_separates_free_and_contact_envelopes(self):
        config = load_config()
        validate_controller_config(config)
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(config["trajectory"]["maximum_translation_per_commit_m"], 1.20)
        self.assertEqual(config["contact"]["maximum_translation_m"], 0.20)
        self.assertEqual(config["contact"]["budget_mode"], "JOINT_6")
        self.assertTrue(
            config["scene_input"][
                "ignore_pushable_for_collision_planning"
            ]
        )
        self.assertEqual(
            config["contact"]["task_torque_budget_nm"],
            [2.0, 2.0, 2.0, 1.0, 1.0, 1.0],
        )
        self.assertEqual(config["runtime"]["interaction_mode"], INTERACTION_ONE_SHOT)
        self.assertEqual(config["runtime"]["ik_mode"], IK_POSITION_3DOF)
        self.assertEqual(config["gripper"]["mode"], "MIT")
        self.assertEqual(
            config["trajectory"]["arrival_cartesian_position_tolerance_m"],
            0.003,
        )
        self.assertEqual(
            config["trajectory"]["arrival_cartesian_orientation_tolerance_rad"],
            0.05,
        )
        self.assertEqual(
            config["trajectory"]["intermediate_arrival_stable_samples"],
            2,
        )
        self.assertEqual(
            config["trajectory"]["one_shot_arrival_settle_timeout_s"],
            1.5,
        )
        self.assertEqual(
            config["trajectory"][
                "one_shot_arrival_settle_kp_multiplier"
            ],
            2.0,
        )
        self.assertEqual(
            config["trajectory"]["authorized_stage_timeout_min_s"],
            3.0,
        )
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

    def test_config_repair_migrates_motion_policy_to_operational_joint_spans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config_templates").mkdir()
            defaults = load_config()
            (root / "config_templates" / "controller.default.json").write_text(
                json.dumps(defaults)
            )
            active = copy.deepcopy(defaults)
            active.pop("managed_policy_revision")
            active["planning"]["maximum_endpoint_joint_delta_rad"] = [
                0.80,
                0.80,
                0.80,
                1.00,
                1.00,
                1.00,
            ]
            active_path = root / "config" / "controller.json"
            active_path.parent.mkdir()
            active_path.write_text(json.dumps(active))

            result = ensure_controller_config(root, active_path)

            self.assertTrue(result.repaired)
            self.assertEqual(result.config["managed_policy_revision"], 6)
            self.assertFalse(
                result.config["workspace"]["enforce_cartesian_bounds"]
            )
            self.assertEqual(
                result.config["planning"][
                    "maximum_transit_joint_velocity_rad_s"
                ],
                20.0,
            )
            self.assertEqual(
                result.config["planning"][
                    "maximum_endpoint_joint_delta_rad"
                ],
                [
                    4.5378560552,
                    5.9341194568,
                    5.9341194568,
                    2.6179938780,
                    2.6179938780,
                    2.6179938780,
                ],
            )
            self.assertEqual(
                result.config["planning"]["maximum_total_joint_travel_rad"],
                1000.0,
            )

            custom = copy.deepcopy(active)
            custom["planning"]["maximum_endpoint_joint_delta_rad"] = [
                0.75,
                0.75,
                0.82,
                0.95,
                0.95,
                0.95,
            ]
            active_path.write_text(json.dumps(custom))
            custom_result = ensure_controller_config(root, active_path)

            self.assertEqual(
                custom_result.config["planning"][
                    "maximum_endpoint_joint_delta_rad"
                ],
                [
                    4.5378560552,
                    5.9341194568,
                    5.9341194568,
                    2.6179938780,
                    2.6179938780,
                    2.6179938780,
                ],
            )


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
            launch_kwargs: dict[str, Any] = {}
            shadow_requests: list[tuple[str, str]] = []
            service.platform.shutdown_plan = (  # type: ignore[method-assign]
                lambda owner_id, reason: (
                    shadow_requests.append((owner_id, reason))
                    or {
                        "state": "PLANNED",
                        "enforcement": "SHADOW_DRY_RUN",
                        "shutdown_id": "shutdown-test",
                    }
                )
            )

            def acknowledged_process(arguments, **kwargs):
                launch_kwargs.update(kwargs)
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
            deadline = time.monotonic() + 1.0
            while not shadow_requests and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(shadow_requests), 1)
            with service.safe_termination_lock:
                manager_shadow_plan = dict(
                    service.safe_termination["manager_shadow_plan"]
                )
            self.assertEqual(
                manager_shadow_plan["enforcement"],
                "SHADOW_DRY_RUN",
            )
            self.assertFalse(
                int(launch_kwargs["creationflags"])
                & int(getattr(subprocess, "DETACHED_PROCESS", 0))
            )
            self.assertTrue(
                int(launch_kwargs["creationflags"])
                & int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            )

    @unittest.skipUnless(os.name == "nt", "Windows helper launch test")
    def test_hidden_powershell_helper_acknowledges_real_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            provider_root = (
                Path(temp) / "providers" / "rebot_arm_integrated"
            )
            scripts = provider_root / "scripts"
            scripts.mkdir(parents=True)
            helper = scripts / "safe_terminate_detached.ps1"
            helper.write_text(
                "\n".join(
                    [
                        "param(",
                        "  [string]$ProjectRoot,",
                        "  [string]$BasicUrl,",
                        "  [string]$IntegratedUrl,",
                        "  [string]$LaunchId",
                        ")",
                        "$logPath = Join-Path "
                        "(Split-Path $PSScriptRoot -Parent) "
                        "'runtime_logs\\safe_terminate.log'",
                        "Add-Content -LiteralPath $logPath -Value "
                        "\"Authoritative safe termination started. "
                        "launch_id=$LaunchId\"",
                    ]
                ),
                encoding="utf-8",
            )
            service = IntegratedService(
                object(),
                load_config(),
                None,
                None,
            )
            service.provider_root = provider_root
            real_popen = subprocess.Popen
            launched_processes: list[subprocess.Popen[Any]] = []

            def capture_process(arguments, **kwargs):
                process = real_popen(arguments, **kwargs)
                launched_processes.append(process)
                return process

            with patch(
                "rebot_arm_integrated.service.subprocess.Popen",
                side_effect=capture_process,
            ):
                result = service.start_safe_termination()
            for process in launched_processes:
                process.wait(timeout=5.0)

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                result["safe_termination"]["state"],
                "RUNNING",
            )


class ControllerTests(unittest.TestCase):
    def test_transit_delta_resolves_from_fresh_measured_controlled_frame(self):
        controller, _ = prepared_controller()
        with controller.lock:
            measured_q = controller._measured_positions_locked()[:6].copy()
            origin = controller.kinematics.controlled_frame(
                measured_q,
                controller._tool_to_control_locked(),
            )

        result = controller.preview_transit_path(
            target_delta_m=[0.01, 0.0, 0.0],
            target_rpy_rad=None,
            requested_speed_m_s=0.03,
        )

        self.assertEqual(
            result["target_resolution"],
            "MEASURED_CONTROLLED_FRAME_PLUS_RELATIVE_DELTA",
        )
        self.assertEqual(result["requested_target_delta_m"], [0.01, 0.0, 0.0])
        self.assertAlmostEqual(
            result["target"]["position_m"][0],
            float(origin[0, 3]) + 0.01,
        )

    def test_default_scene_policy_keeps_pushable_geometry_non_blocking(self):
        controller, _ = prepared_controller()
        with controller.lock:
            self.assertTrue(
                controller._pushable_contact_is_permitted_locked(False)
            )
            controller.config["scene_input"][
                "ignore_pushable_for_collision_planning"
            ] = False
            self.assertFalse(
                controller._pushable_contact_is_permitted_locked(False)
            )
            self.assertTrue(
                controller._pushable_contact_is_permitted_locked(True)
            )

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

    def test_warm_serializes_against_inflight_lease_renewal(self):
        controller, basic = prepared_controller()
        renew_started = threading.Event()
        finish_renew = threading.Event()
        original_renew = basic.renew

        def slow_renew(duration_ms):
            renew_started.set()
            self.assertTrue(finish_renew.wait(1.0))
            return original_renew(duration_ms)

        basic.renew = slow_renew
        renew_result = []
        warm_result = []
        renew_worker = threading.Thread(
            target=lambda: renew_result.append(controller._renew_lease_once())
        )
        warm_worker = threading.Thread(
            target=lambda: warm_result.append(controller.enter_warm())
        )
        renew_worker.start()
        self.assertTrue(renew_started.wait(0.5))
        warm_worker.start()
        time.sleep(0.03)
        self.assertTrue(warm_worker.is_alive())
        finish_renew.set()
        renew_worker.join(1.0)
        warm_worker.join(1.0)

        self.assertEqual(renew_result, [True])
        self.assertEqual(warm_result, [{"status": "warm"}])
        self.assertEqual(controller.residency, "WARM")
        self.assertEqual(controller.lease_state, "NONE")
        self.assertIsNone(basic.lease_snapshot())
        self.assertIsNone(controller.fault_reason)

    def test_lease_loss_requires_explicit_recovery_and_stops_background_reacquire(self):
        controller, basic = prepared_controller()
        controller.start()
        original_generation = basic.generation
        controller._handle_lease_loss(
            "NO_ACTIVE_LEASE: safe-home preempted operational control"
        )

        time.sleep(1.2)

        state = controller.snapshot()
        self.assertEqual(state["residency"], "RECOVERY_REQUIRED")
        self.assertFalse(state["ready"])
        self.assertEqual(state["lease"]["state"], "LOST")
        self.assertIsNone(basic.lease_snapshot())
        self.assertEqual(basic.generation, original_generation)
        controller.stop()

    def test_explicit_hot_transition_recovers_after_lease_loss(self):
        controller, basic = prepared_controller()
        original_generation = basic.generation
        controller._handle_lease_loss(
            "NO_ACTIVE_LEASE: safe-home preempted operational control"
        )

        result = controller.enter_hot()

        self.assertEqual(result["status"], "hot_target_edit")
        state = controller.snapshot()
        self.assertEqual(state["residency"], "HOT")
        self.assertTrue(state["ready"])
        self.assertEqual(state["lease"]["state"], "OWNED")
        self.assertEqual(basic.generation, original_generation + 1)
        controller.stop()

    def test_press_transit_and_contact_are_physically_enabled_backends(self):
        controller, _ = prepared_controller()
        state = controller.snapshot()
        self.assertEqual(state["control_mode"], CONTROL_MODE)
        self.assertEqual(state["execution_mode"], TRANSIT_SPEED)
        self.assertEqual(
            state["basic_execution_mode"],
            "POSITION_VELOCITY_LIMITED",
        )
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
            readiness["robot.motion.arm.integrated.pos_vel.one_shot"]
        )
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
                "robot.motion.arm.integrated.pos_vel.one_shot"
            ]["constraints"],
            {
                "maximum_path_length_m": 1.2,
                "load": "NO_PAYLOAD_OR_HIGH_EXTERNAL_LOAD",
                "effective_joint_speed_policy": (
                    "MIN_CONTROLLER_BASIC_POS_SPEED_AND_MOTOR_VMAX"
                ),
            },
        )
        self.assertEqual(
            state["capability_profiles"][
                "robot.motion.arm.integrated.pos_vel.one_shot_limited"
            ]["replacement"],
            "robot.motion.arm.integrated.pos_vel.one_shot",
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
        self.assertIn("robot.motion.arm.integrated.pos_vel.one_shot", names)
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

    def test_twenty_five_cm_up_preserving_safe_home_orientation_is_previewable(self):
        controller, basic = prepared_controller()
        basic._state["positions_rad"][:6] = [0.0] * 6
        controller.set_runtime_settings({"ik_mode": IK_POSE_6DOF})
        with controller.lock:
            controller.basic_state = basic.state()
            origin = controller.kinematics.controlled_frame(
                np.zeros(6),
                controller._tool_to_control_locked(),
            )
            controller.staged_target = origin.copy()
            controller.staged_target[2, 3] += 0.25

        preview = controller.preview_staged_target()

        self.assertTrue(preview["planning_valid"])
        self.assertEqual(preview["ik_mode"], IK_POSE_6DOF)
        self.assertGreater(preview["endpoint_joint_delta_rad"][2], 1.0)
        self.assertLessEqual(
            preview["endpoint_joint_delta_rad"][2],
            preview["endpoint_joint_delta_limit_rad"][2],
        )
        self.assertEqual(basic.commands, [])

    def test_zero_length_preview_does_not_report_a_singularity(self):
        controller, basic = prepared_controller()
        q = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        result = type(
            "IkResult",
            (),
            {
                "q_goal": q.copy(),
                "position_residual_m": 0.0,
                "orientation_residual_rad": 0.0,
                "iterations": 1,
                "sigma_min": 0.0,
            },
        )()
        with patch.object(
            controller,
            "_solve_target_locked",
            return_value=result,
        ):
            preview = controller.preview_staged_target()

        self.assertTrue(preview["planning_valid"])
        self.assertFalse(
            any(
                "singular" in reason.lower()
                for reason in preview["planning_reasons"]
            )
        )
        self.assertEqual(basic.commands, [])

    def test_preview_rejects_large_ik_position_residual(self):
        controller, basic = prepared_controller()
        q = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        controller.staged_target[0, 3] += 0.002
        result = type(
            "IkResult",
            (),
            {
                "q_goal": q.copy(),
                "position_residual_m": 0.102,
                "orientation_residual_rad": 0.0,
                "iterations": 180,
                "sigma_min": 0.02,
            },
        )()
        with patch.object(
            controller,
            "_solve_target_locked",
            return_value=result,
        ):
            preview = controller.preview_staged_target()

        self.assertFalse(preview["planning_valid"])
        self.assertTrue(
            any(
                "position residual" in reason
                for reason in preview["planning_reasons"]
            )
        )
        self.assertEqual(basic.commands, [])

    def test_physical_commit_rejects_fresh_large_ik_residual(self):
        controller, basic = prepared_controller()
        controller.set_engaged(True)
        q = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        controller.staged_target[0, 3] += 0.002
        result = type(
            "IkResult",
            (),
            {
                "q_goal": q.copy(),
                "position_residual_m": 0.102,
                "orientation_residual_rad": 0.0,
                "iterations": 180,
                "sigma_min": 0.02,
            },
        )()
        floats_before = basic.float_count
        with patch.object(
            controller,
            "_solve_target_locked",
            return_value=result,
        ):
            controller._commit_staged_target()

        self.assertIsNone(controller.trajectory)
        self.assertEqual(basic.commands, [])
        self.assertGreater(basic.float_count, floats_before)
        self.assertIn("IK position residual", controller.last_error)
        self.assertEqual(
            controller.snapshot()["target"]["residual_policy"],
            "PREVIEW_AND_EXECUTION_REJECTION",
        )

    def test_transit_path_shadow_does_not_stage_engage_or_command(self):
        controller, basic = prepared_controller()
        before = controller.snapshot()
        target = controller.staged_target[:3, 3].copy()
        target[1] += 0.01

        result = controller.preview_transit_path(
            target_position_m=target.tolist(),
            target_rpy_rad=None,
            requested_speed_m_s=0.4,
        )

        after = controller.snapshot()
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(
            result["planner_owner"],
            "ROBOT_ARM_INTEGRATED_CONTROLLER",
        )
        self.assertEqual(result["enforcement"], "SHADOW_NONPHYSICAL")
        self.assertTrue(result["control_state_unchanged"])
        self.assertTrue(result["lease_unchanged"])
        self.assertEqual(before["control_state"], after["control_state"])
        self.assertEqual(before["engaged"], after["engaged"])
        self.assertEqual(before["lease"]["lease_id"], after["lease"]["lease_id"])
        self.assertEqual(before["target"]["staged"], after["target"]["staged"])
        self.assertEqual(basic.commands, [])

    def test_transit_path_preview_is_available_while_warm_and_unleased(self):
        controller, basic = prepared_controller()
        controller.enter_warm()
        before = controller.snapshot()
        target = controller.staged_target[:3, 3].copy()
        target[1] += 0.01
        float_count = basic.float_count

        result = controller.preview_transit_path(
            target_position_m=target.tolist(),
            target_rpy_rad=None,
            requested_speed_m_s=0.05,
        )

        after = controller.snapshot()
        self.assertEqual(result["status"], "PLANNED")
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(before["residency"], "WARM")
        self.assertEqual(after["residency"], "WARM")
        self.assertFalse(after["ready"])
        self.assertIsNone(basic.lease_snapshot())
        self.assertEqual(basic.float_count, float_count)
        self.assertEqual(basic.commands, [])

    def test_transit_path_preview_stops_at_shadow_planning_time_budget(self):
        controller, basic = prepared_controller()
        controller.config["planning"]["shadow_planning_time_budget_s"] = 0.001
        target = controller.staged_target[:3, 3].copy()
        target[1] += 0.01
        original_solve = controller._solve_target_locked

        def slow_solve(*args, **kwargs):
            time.sleep(0.01)
            return original_solve(*args, **kwargs)

        with patch.object(
            controller,
            "_solve_target_locked",
            side_effect=slow_solve,
        ):
            result = controller.preview_transit_path(
                target_position_m=target.tolist(),
                target_rpy_rad=None,
                requested_speed_m_s=0.05,
            )

        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["planning_timed_out"])
        self.assertEqual(len(result["candidate_evaluations"]), 1)
        self.assertIn(
            "SHADOW_PLANNING_TIME_BUDGET_EXCEEDED",
            result["selected_plan"]["planning_reasons"],
        )
        self.assertEqual(basic.commands, [])

    def test_transit_path_preview_lazily_loads_model_in_initial_warm_state(self):
        config = load_config()
        basic = FakeBasic()
        controller = IntegratedController(config, basic)
        controller.update_platform_status(
            True,
            True,
            {},
            motion_inhibited=True,
        )
        model = ArmKinematics(basic.model())
        q = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        target = model.controlled_frame(
            q,
            controller._tool_to_control_locked(),
        )[:3, 3]
        target[1] += 0.01

        result = controller.preview_transit_path(
            target_position_m=target.tolist(),
            target_rpy_rad=None,
            requested_speed_m_s=0.05,
        )

        self.assertEqual(result["status"], "PLANNED")
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(controller.residency, "WARM")
        self.assertFalse(controller.ready)
        self.assertIsNotNone(controller.kinematics)
        self.assertIsNone(basic.lease_snapshot())
        self.assertEqual(basic.commands, [])

    def test_authorized_transit_executes_bounded_stages_and_holds_final(
        self,
    ):
        controller, basic = prepared_controller()
        controller.stage_scene(
            {
                "scene_revision": "scene-authorized-1",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test",
        )
        q_start = np.asarray(
            basic._state["positions_rad"][:6],
            dtype=float,
        )
        q_mid = q_start.copy()
        q_mid[0] += 0.01
        q_goal = q_mid.copy()
        q_goal[1] -= 0.01
        original_command = basic.command

        def follow_endpoint(commands, timeout_ms=250):
            result = original_command(commands, timeout_ms)
            goal = np.asarray(
                [
                    float(command["values"]["position_rad"])
                    for command in commands
                    if int(command["joint_index"]) < 6
                ],
                dtype=float,
            )
            for command in commands:
                index = int(command["joint_index"])
                if index < 6 and "position_rad" in command["values"]:
                    basic._state["positions_rad"][index] = float(
                        command["values"]["position_rad"]
                    )
                    basic._state["velocities_rad_s"][index] = (
                        0.05
                        if np.allclose(goal, q_mid)
                        else 0.0
                    )
            return result

        basic.command = follow_endpoint  # type: ignore[method-assign]
        controller.stage_scene(
            {
                "scene_revision": "scene-authorized-2",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test-refresh-before-commit",
        )
        result = controller.execute_authorized_transit(
            plan_id="plan-authorized-1",
            preview_sha256="preview-sha",
            request_sha256="request-sha",
            q_waypoints_rad=[
                q_start.tolist(),
                q_mid.tolist(),
                q_goal.tolist(),
            ],
            requested_speed_m_s=0.05,
            scene_revision="scene-authorized-1",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-1",
                "decision_id": "decision-1",
                "resolved_by": "operator",
            },
        )

        self.assertEqual(result["status"], "EXECUTING")
        self.assertEqual(
            result["preview_scene_revision"],
            "scene-authorized-1",
        )
        self.assertEqual(
            result["commit_scene_revision"],
            "scene-authorized-2",
        )
        self.assertTrue(result["scene_revision_advanced"])
        self.assertLessEqual(
            result["maximum_joint_velocity_rad_s"],
            10.0,
        )
        self.assertTrue(
            np.allclose(
                result["joint_speed_policy"]["joint_rate_caps_rad_s"],
                [5.0, 5.0, 5.0, 10.0, 10.0, 10.0],
                rtol=0.0,
                atol=1e-12,
            )
        )
        self.assertTrue(
            wait_until(
                lambda: (
                    controller.authorized_transit is not None
                    and controller.authorized_transit.status
                    == "HOLDING_FINAL"
                ),
                timeout=2.0,
            )
        )
        self.assertTrue(basic.commands)
        for envelope in basic.commands:
            for command in envelope:
                if command["joint_index"] < 6:
                    self.assertEqual(command["mode"], "IMPEDANCE")
                    self.assertLessEqual(
                        command["values"]["target_rate_limit_rad_s"],
                        0.25,
                    )
                    self.assertGreater(command["values"]["kp"], 0.0)
                    self.assertIn(
                        "feedforward_torque_nm",
                        command["values"],
                    )
        summary = controller.snapshot()["planning"]["authorized_transit"]
        self.assertEqual(summary["arm_command_mode"], "IMPEDANCE")
        self.assertEqual(summary["minimum_kp_multiplier"], 1.0)
        self.assertEqual(
            summary["gravity_feedforward_owner"],
            "ROBOT_ARM_BASIC_CALIBRATED_ARM_AND_DECLARED_PAYLOAD",
        )
        floats_before_release = basic.float_count
        released = controller.release_authorized_transit()
        self.assertEqual(released["status"], "gravity_float")
        self.assertIsNone(controller.authorized_transit)
        self.assertGreater(basic.float_count, floats_before_release)

    def test_wait_for_next_chains_without_intermediate_float_then_floats(self):
        controller, basic = prepared_controller()
        controller.stage_scene(
            {
                "scene_revision": "scene-chain-1",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test",
        )
        original_command = basic.command

        def follow_endpoint(commands, timeout_ms=250):
            result = original_command(commands, timeout_ms)
            for command in commands:
                index = int(command["joint_index"])
                if index < 6 and "position_rad" in command["values"]:
                    basic._state["positions_rad"][index] = float(
                        command["values"]["position_rad"]
                    )
                    basic._state["velocities_rad_s"][index] = 0.0
            return result

        basic.command = follow_endpoint  # type: ignore[method-assign]
        q_start = np.asarray(basic._state["positions_rad"][:6], dtype=float)
        q_first = q_start.copy()
        q_first[0] += 0.01
        first = controller.execute_authorized_transit(
            plan_id="plan-chain-1",
            preview_sha256="preview-chain-1",
            request_sha256="request-chain-1",
            q_waypoints_rad=[q_start.tolist(), q_first.tolist()],
            requested_speed_m_s=0.05,
            scene_revision="scene-chain-1",
            final_state="WAIT_FOR_NEXT",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-chain-1",
                "decision_id": "decision-chain-1",
                "resolved_by": "operator",
            },
        )
        self.assertEqual(first["final_state"], "WAIT_FOR_NEXT")
        self.assertTrue(
            wait_until(
                lambda: (
                    controller.authorized_transit is not None
                    and controller.authorized_transit.status
                    == "WAITING_NEXT"
                ),
                timeout=2.0,
            )
        )
        float_count_during_wait = basic.float_count

        q_second = q_first.copy()
        q_second[1] -= 0.01
        second = controller.execute_authorized_transit(
            plan_id="plan-chain-2",
            preview_sha256="preview-chain-2",
            request_sha256="request-chain-2",
            q_waypoints_rad=[q_first.tolist(), q_second.tolist()],
            requested_speed_m_s=0.05,
            scene_revision="scene-chain-1",
            final_state="FLOAT",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-chain-2",
                "decision_id": "decision-chain-2",
                "resolved_by": "operator",
            },
        )

        self.assertEqual(second["chained_from_plan_id"], "plan-chain-1")
        self.assertEqual(basic.float_count, float_count_during_wait)
        self.assertTrue(
            wait_until(
                lambda: (
                    controller.authorized_transit is None
                    and controller.last_authorized_transit is not None
                    and controller.last_authorized_transit["status"]
                    == "COMPLETED_FLOAT"
                ),
                timeout=3.0,
            )
        )
        self.assertGreater(basic.float_count, float_count_during_wait)
        self.assertTrue(controller.snapshot()["safety"]["float_confirmed"])

    def test_gripper_can_join_authorized_final_hold_without_float(self):
        controller, basic = prepared_controller()
        controller.stage_scene(
            {
                "scene_revision": "scene-authorized-gripper-hold",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test",
        )
        q_start = np.asarray(
            basic._state["positions_rad"][:6],
            dtype=float,
        )
        q_goal = q_start.copy()
        q_goal[0] += 0.01
        original_command = basic.command

        def follow_endpoint(commands, timeout_ms=250):
            result = original_command(commands, timeout_ms)
            for command in commands:
                index = int(command["joint_index"])
                if index < 6 and "position_rad" in command["values"]:
                    basic._state["positions_rad"][index] = float(
                        command["values"]["position_rad"]
                    )
                    basic._state["velocities_rad_s"][index] = 0.0
            return result

        basic.command = follow_endpoint  # type: ignore[method-assign]
        controller.execute_authorized_transit(
            plan_id="plan-authorized-gripper-hold",
            preview_sha256="preview-sha",
            request_sha256="request-sha",
            q_waypoints_rad=[
                q_start.tolist(),
                q_goal.tolist(),
            ],
            requested_speed_m_s=0.05,
            scene_revision="scene-authorized-gripper-hold",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-gripper-hold",
                "decision_id": "decision-gripper-hold",
                "resolved_by": "operator",
            },
        )
        self.assertTrue(
            wait_until(
                lambda: (
                    controller.authorized_transit is not None
                    and controller.authorized_transit.status
                    == "HOLDING_FINAL"
                ),
                timeout=2.0,
            )
        )

        floats_before_gripper = basic.float_count
        accepted = controller.request_gripper("OPEN")
        self.assertTrue(accepted["accepted"])
        self.assertTrue(
            wait_until(
                lambda: any(
                    any(
                        int(command["joint_index"]) == 6
                        and command["values"].get("position_rad")
                        == controller.gripper_open_position_rad
                        for command in envelope
                    )
                    and any(
                        int(command["joint_index"]) < 6
                        for command in envelope
                    )
                    for envelope in basic.commands
                ),
                timeout=1.0,
            )
        )
        self.assertEqual(basic.float_count, floats_before_gripper)
        self.assertIsNotNone(controller.authorized_transit)
        self.assertEqual(
            controller.authorized_transit.status,
            "HOLDING_FINAL",
        )
        self.assertEqual(
            controller.snapshot()["gripper"]["active_action"],
            "OPEN",
        )
        command_count_before_stop = len(basic.commands)
        stopped = controller.request_gripper("STOP")
        self.assertTrue(stopped["accepted"])
        self.assertIsNone(stopped["requested_action"])
        self.assertTrue(
            wait_until(
                lambda: any(
                    any(
                        int(command["joint_index"]) < 6
                        for command in envelope
                    )
                    and all(
                        int(command["joint_index"]) != 6
                        for command in envelope
                    )
                    for envelope in basic.commands[command_count_before_stop:]
                ),
                timeout=1.0,
            )
        )
        stopped_snapshot = controller.snapshot()["gripper"]
        self.assertIsNone(stopped_snapshot["active_action"])
        self.assertIsNone(stopped_snapshot["target_rad"])
        self.assertEqual(stopped_snapshot["stop_count"], 1)
        self.assertEqual(basic.float_count, floats_before_gripper)
        self.assertIsNotNone(controller.authorized_transit)
        self.assertEqual(
            controller.authorized_transit.status,
            "HOLDING_FINAL",
        )
        controller.release_authorized_transit()

    def test_gripper_remains_blocked_during_authorized_transit(self):
        controller, basic = prepared_controller()
        controller.stage_scene(
            {
                "scene_revision": "scene-authorized-gripper-block",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test",
        )
        q_start = np.asarray(
            basic._state["positions_rad"][:6],
            dtype=float,
        )
        q_goal = q_start.copy()
        q_goal[0] += 0.2
        controller.execute_authorized_transit(
            plan_id="plan-authorized-gripper-block",
            preview_sha256="preview-sha",
            request_sha256="request-sha",
            q_waypoints_rad=[
                q_start.tolist(),
                q_goal.tolist(),
            ],
            requested_speed_m_s=0.05,
            scene_revision="scene-authorized-gripper-block",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-gripper-block",
                "decision_id": "decision-gripper-block",
                "resolved_by": "operator",
            },
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "blocked while an arm trajectory is active",
        ):
            controller.request_gripper("OPEN")
        controller.release_authorized_transit()

    def test_authorized_transit_final_endpoint_requires_settled_velocity(
        self,
    ):
        controller, basic = prepared_controller()
        controller.config["trajectory"]["arrival_stable_samples"] = 2
        controller.stage_scene(
            {
                "scene_revision": "scene-authorized-final-settle",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
            source="test",
        )
        q_start = np.asarray(
            basic._state["positions_rad"][:6],
            dtype=float,
        )
        q_goal = q_start.copy()
        q_goal[0] += 0.01
        original_command = basic.command
        report_moving_velocity = threading.Event()
        report_moving_velocity.set()

        def follow_moving_endpoint(commands, timeout_ms=250):
            result = original_command(commands, timeout_ms)
            for command in commands:
                index = int(command["joint_index"])
                if index < 6 and "position_rad" in command["values"]:
                    basic._state["positions_rad"][index] = float(
                        command["values"]["position_rad"]
                    )
                    if report_moving_velocity.is_set():
                        basic._state["velocities_rad_s"][index] = 0.05
            return result

        basic.command = (  # type: ignore[method-assign]
            follow_moving_endpoint
        )
        controller.execute_authorized_transit(
            plan_id="plan-authorized-final-settle",
            preview_sha256="preview-sha",
            request_sha256="request-sha",
            q_waypoints_rad=[
                q_start.tolist(),
                q_goal.tolist(),
            ],
            requested_speed_m_s=0.05,
            scene_revision="scene-authorized-final-settle",
            allowed_contact_object_ids=set(),
            permit_pushable_contact=False,
            authorization_claims={
                "assertion_id": "assertion-final-settle",
                "decision_id": "decision-final-settle",
                "resolved_by": "operator",
            },
        )

        self.assertFalse(
            wait_until(
                lambda: (
                    controller.authorized_transit is not None
                    and controller.authorized_transit.status
                    == "HOLDING_FINAL"
                ),
                timeout=0.5,
            )
        )
        report_moving_velocity.clear()
        basic._state["velocities_rad_s"][:6] = [0.0] * 6
        self.assertTrue(
            wait_until(
                lambda: (
                    controller.authorized_transit is not None
                    and controller.authorized_transit.status
                    == "HOLDING_FINAL"
                ),
                timeout=1.0,
            )
        )
        controller.release_authorized_transit()

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
        time.sleep(0.08)
        self.assertIsNotNone(controller.trajectory)
        self.assertTrue(wait_until(lambda: controller.trajectory is None, timeout=2.0))
        modes = {item["mode"] for frame in basic.commands for item in frame}
        self.assertIn("POSITION_VELOCITY_LIMITED", modes)
        self.assertNotIn(MODE_MIT, modes)
        self.assertGreater(basic.float_count, floats_before_commit)
        self.assertTrue(
            controller.last_completed_trajectory[
                "arrival_cartesian_confirmed"
            ]
        )
        self.assertTrue(
            controller.last_completed_trajectory[
                "arrival_duration_confirmed"
            ]
        )

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
        controller.set_runtime_settings({"execution_mode": PRESS_MIT})
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
        base_gains = controller._gain_profile_locked(1.0)
        settle_gains = controller._gain_profile_locked(2.0)
        self.assertTrue(
            any(
                frame
                and frame[0]["values"]["kp"]
                == settle_gains[0]["effective_kp"]
                and frame[0]["values"]["kp"]
                > base_gains[0]["effective_kp"]
                for frame in basic.commands
            )
        )
        completed = controller.last_completed_trajectory
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(
            completed["completion_outcome"],
            "ARRIVAL_CONFIRMED_AND_FLOATED",
        )
        self.assertTrue(completed["completion_success"])
        self.assertTrue(completed["target_arrival_confirmed"])
        self.assertTrue(completed["arrival_duration_confirmed"])
        self.assertTrue(completed["deadline_joint_within_tolerance"])
        self.assertIsNotNone(completed["deadline_position_residual_m"])

    def test_one_shot_deadline_reports_incomplete_when_arm_does_not_follow(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"execution_mode": PRESS_MIT})
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[2, 3] += 0.02
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()

        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        completed = controller.last_completed_trajectory
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(
            completed["completion_outcome"],
            "DEADLINE_FLOAT_BEFORE_ARRIVAL",
        )
        self.assertFalse(completed["completion_success"])
        self.assertFalse(completed["target_arrival_confirmed"])
        self.assertTrue(completed["arrival_duration_confirmed"])
        self.assertFalse(completed["deadline_cartesian_within_tolerance"])
        self.assertGreater(completed["deadline_position_residual_m"], 0.003)
        self.assertGreater(basic.float_count, 0)

    def test_rejected_optional_preview_does_not_veto_operator_commit(self):
        controller, _ = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"execution_mode": PRESS_MIT})
        controller.set_engaged(True)
        with controller.lock:
            origin = controller.kinematics.controlled_frame(controller._measured_positions_locked()[:6], controller._tool_to_control_locked())
            controller.staged_target = origin.copy()
            controller.staged_target[1, 3] += 0.005
            target_point = controller.staged_target[:3, 3].tolist()
        controller.stage_scene(
            {
                "scene_revision": "optional-diagnostic-collision",
                "frame_id": "rebot_arm_base",
                "spheres": [
                    {
                        "sphere_id": "diagnostic-only",
                        "object_id": "diagnostic-only",
                        "center_m": target_point,
                        "radius_m": 0.05,
                        "type": "KEEP_OUT",
                    }
                ],
            },
            source="test",
        )
        preview = controller.preview_staged_target()
        self.assertFalse(preview["planning_valid"])
        self.assertIn(
            "candidate path intersects a non-permitted semantic object",
            preview["planning_reasons"],
        )
        controller.update_input({"lb": True})
        controller._tick()
        self.assertIsNotNone(controller.last_committed_target)

    def test_all_motion_commands_are_impedance(self):
        controller, basic = prepared_controller(short_trajectory=True)
        controller.set_runtime_settings({"execution_mode": PRESS_MIT})
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target[1, 3] += 0.005
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: controller.trajectory is None))
        self.assertTrue(basic.commands)
        self.assertEqual({item["mode"] for frame in basic.commands for item in frame}, {MODE_MIT})

    def test_press_mit_recovers_when_measured_start_is_outside_operational_range(self):
        config = load_config()
        config["runtime"]["duration_s"] = 0.25
        config["trajectory"]["send_rate_hz"] = 100.0
        basic = FakeBasic()
        joint4_low = float(
            basic._model["joints"][3]["operational_limit_rad"][0]
        )
        basic._state["positions_rad"][3] = joint4_low - 0.08
        controller = IntegratedController(config, basic)
        controller.enter_hot()
        controller.set_runtime_settings({"execution_mode": PRESS_MIT})
        controller.update_platform_status(
            True,
            True,
            {},
            motion_inhibited=False,
        )
        controller.set_engaged(True)
        with controller.lock:
            controller.staged_target = controller.kinematics.controlled_frame(
                controller._measured_positions_locked()[:6],
                controller._tool_to_control_locked(),
            )
            controller.staged_target[2, 3] += 0.002
        controller.preview_staged_target()
        controller.update_input({"lb": True})
        controller._tick()
        self.assertTrue(wait_until(lambda: len(basic.commands) >= 1))
        first_arm_frame = basic.commands[0][:6]
        for index, command in enumerate(first_arm_frame):
            low, high = basic._model["joints"][index][
                "operational_limit_rad"
            ]
            target = float(command["values"]["position_rad"])
            self.assertGreaterEqual(target, float(low))
            self.assertLessEqual(target, float(high))
        state = controller.snapshot()
        self.assertGreater(
            state["trajectory"]["operational_range_recovery_count"],
            0,
        )
        self.assertEqual(
            state["trajectory"][
                "last_operational_range_recovery_joint_indices"
            ],
            [3],
        )
        controller.request_float()

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

    def test_leased_compliant_idle_hold_uses_requested_multiplier(self):
        controller, basic = prepared_controller()
        result = controller.set_idle_profile(
            {
                "profile": IDLE_COMPLIANT_HOLD,
                "holder": "skill.test",
                "lease_duration_ms": 3000,
                "kp_multiplier": 3.0,
            }
        )

        self.assertEqual(result["profile"], IDLE_COMPLIANT_HOLD)
        controller._service_idle_profile(time.monotonic())
        self.assertEqual(len(basic.commands), 1)
        commands = basic.commands[-1]
        self.assertTrue(
            all(command["mode"] == "IMPEDANCE" for command in commands[:6])
        )
        base_gains = controller._gain_profile_locked(1.0)
        held_gains = [
            command["values"]["kp"] for command in commands[:6]
        ]
        self.assertEqual(
            held_gains,
            [
                controller._gain_profile_locked(3.0)[index]["effective_kp"]
                for index in range(6)
            ],
        )
        self.assertTrue(
            all(
                held >= base["effective_kp"]
                for held, base in zip(held_gains, base_gains)
            )
        )
        renewed = controller.renew_idle_profile(
            {
                "profile_lease_id": result["profile_lease_id"],
                "holder": "skill.test",
                "lease_duration_ms": 3000,
            }
        )
        self.assertGreater(renewed["expires_in_ms"], 0)
        controller.release_idle_profile(
            {
                "profile_lease_id": result["profile_lease_id"],
                "holder": "skill.test",
            }
        )
        self.assertEqual(
            controller.idle_profile_snapshot()["profile"],
            "GRAVITY_FLOAT",
        )

    def test_leased_position_lock_sends_pos_vel_and_expires_to_float(self):
        controller, basic = prepared_controller()
        result = controller.set_idle_profile(
            {
                "profile": IDLE_POSITION_LOCK,
                "holder": "skill.test",
                "lease_duration_ms": 3000,
            }
        )

        controller._service_idle_profile(time.monotonic())
        self.assertEqual(len(basic.commands), 1)
        self.assertTrue(
            all(
                command["mode"] == "POSITION_VELOCITY_LIMITED"
                for command in basic.commands[-1][:6]
            )
        )
        float_count = basic.float_count
        with controller.lock:
            controller.idle_profile_expires_monotonic = time.monotonic() - 1.0
        controller._service_idle_profile(time.monotonic())
        self.assertGreater(basic.float_count, float_count)
        self.assertEqual(controller.control_state, "TARGET_EDIT")
        self.assertEqual(
            controller.idle_profile_snapshot()["profile"],
            "GRAVITY_FLOAT",
        )
        with self.assertRaises(PermissionError):
            controller.renew_idle_profile(
                {
                    "profile_lease_id": result["profile_lease_id"],
                    "holder": "skill.test",
                    "lease_duration_ms": 3000,
                }
            )

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
