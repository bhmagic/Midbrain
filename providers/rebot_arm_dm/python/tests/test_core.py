from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import types
import sys
import unittest
from unittest.mock import patch
import numpy as np

from rebot_arm_dm_provider.calibration import fit_two_parameter_friction, robust_fit
from rebot_arm_dm_provider.assembly import AssemblyConfigurationError, RobotAssemblyConfiguration
from rebot_arm_dm_provider.collision import CalibrationCollisionGuard
from rebot_arm_dm_provider.controller import ArmController, CommandEnvelope, JointCommand, LeasePermissionError, ProviderState
from rebot_arm_dm_provider.dynamics import RebotDynamics
from rebot_arm_dm_provider.hardware import MotorBridgeBackend, SimulationBackend
from rebot_arm_dm_provider.kinematics import RebotKinematics
from rebot_arm_dm_provider.models import ArmConfiguration
from rebot_arm_dm_provider.service import ArmProviderService

ROOT=Path(os.environ.get('REBOT_PROVIDER_ROOT',Path(__file__).resolve().parents[2]))


def configuration():
    return ArmConfiguration.load(ROOT/'config_templates'/'arm_model.factory.json',ROOT/'config_templates'/'arm_calibration.initial.json')


def wait_for(predicate, timeout=1.5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class AssemblyConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ROOT.parents[1]
        self.selection_path = (
            self.workspace / 'config' / 'robot_assemblies' /
            'primary_manipulator.example.json'
        )

    def test_provider_owned_profiles_resolve_to_normalized_resource_groups(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        state = assembly.public_state()
        self.assertEqual(state['schema'], 'midbrain.robot_assembly_state')
        self.assertEqual(state['arm_model_identity']['model_id'], 'rebot_arm_b601_dm')
        groups = {item['group_id']: item for item in state['resource_groups']}
        self.assertEqual(groups['arm']['joint_names'], [
            'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'
        ])
        self.assertNotIn('gripper', groups)
        self.assertEqual(
            state['mounted_effector']['profile_id'],
            'rebot_b601_dm.5_inch_blade',
        )
        self.assertEqual(
            state['mounted_effector']['inactive_joint_names'],
            ['gripper'],
        )
        self.assertNotIn(
            'mounted_effector_compatibility',
            state['collision_geometry'],
        )
        self.assertEqual(
            [
                (
                    item['transform']['translation_m'],
                    item['shape']['radius_m'],
                )
                for item in state['mounted_effector']['collision_primitives']
            ],
            [
                ([-0.005, 0.0, -0.08], 0.005),
                ([-0.03, 0.0, -0.08], 0.015),
                ([-0.09, 0.0, -0.08], 0.035),
                ([-0.15, 0.0, -0.08], 0.035),
            ],
        )
        normalized = assembly.normalized_arm_model()
        self.assertEqual(normalized['frames']['tool'], 'rebot_arm_tool')
        self.assertEqual(normalized['fixed_tool']['parent_link'], 'link6')
        self.assertEqual(normalized['fixed_tool']['child_link'], 'end_link')
        inertial = state['mounted_effector']['inertial']
        self.assertEqual(inertial['mass_kg'], 0.33)
        self.assertEqual(
            inertial['center_of_mass_m'],
            [-0.165, 0.0, -0.03],
        )
        self.assertEqual(
            normalized['links'][-1]['mass_kg'],
            inertial['mass_kg'],
        )
        self.assertEqual(
            normalized['links'][-1]['center_of_mass_m'],
            inertial['center_of_mass_m'],
        )
        self.assertAlmostEqual(
            normalized['links'][-1]['weight_n_at_standard_gravity'],
            float(inertial['mass_kg']) * 9.80665,
        )

    def test_inactive_effector_joint_is_not_exposed_as_arm_or_effector_resource(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )

        self.assertEqual(assembly.arm_joint_names, (
            'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'
        ))
        self.assertEqual(assembly.inactive_joint_names, ('gripper',))
        self.assertEqual(
            [group['group_id'] for group in assembly.resource_groups()],
            ['arm'],
        )

    def test_fixed_tool_service_starts_without_registering_inactive_gripper(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        configuration = ArmConfiguration(
            assembly.normalized_arm_model(),
            assembly.profiles['calibration'],
        )
        kinematics = RebotKinematics(configuration.model)
        dynamics = RebotDynamics(configuration, kinematics)
        backend = SimulationBackend(
            configuration,
            dynamics.calibrated_gravity_torque,
        )
        controller = ArmController(configuration, backend, dynamics)
        service = ArmProviderService(
            configuration,
            controller,
            kinematics,
            assembly.calibration_path,
            '127.0.0.1',
            0,
            None,
            None,
            False,
            True,
            False,
            assembly,
        )

        self.assertEqual(controller.active_joint_indices, (0, 1, 2, 3, 4, 5))
        self.assertEqual(controller.inactive_joint_indices, frozenset({6}))
        self.assertEqual(backend.inactive_joint_indices, frozenset({6}))

        service.start()
        try:
            state = controller.snapshot()
            self.assertEqual(
                state['provider_state'],
                ProviderState.SAFE_HOLD_GRAVITY_FLOAT.value,
            )
            self.assertEqual(state['motor_status'][6], 'INACTIVE_NOT_INSTALLED')
            self.assertEqual(state['inactive_joint_names'], ['gripper'])
            self.assertIsNone(state['active_command_modes'][6])
            lease = controller.acquire_lease('fixed-tool-test', 1000)
            with self.assertRaisesRegex(
                LeasePermissionError,
                'inactive joint indices',
            ):
                controller.submit(CommandEnvelope(
                    'inactive-gripper-command',
                    lease.lease_id,
                    lease.fencing_generation,
                    {
                        6: JointCommand(
                            'POSITION_EFFORT_LIMITED',
                            {
                                'position_rad': 0.0,
                                'velocity_limit_rad_s': 0.1,
                                'torque_limit_ratio': 0.1,
                            },
                        ),
                    },
                    time.monotonic() + 0.5,
                ))
        finally:
            service.shutdown(False)

    def test_joint_cannot_be_both_inactive_and_actuated(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        assembly.profiles['mounted_effector']['actuator_groups'] = [
            {
                'group_id': 'invalid',
                'resource_id': 'robot_arm.primary/invalid',
                'joint_names': ['gripper'],
                'capabilities': ['invalid'],
            }
        ]

        with self.assertRaisesRegex(
            AssemblyConfigurationError,
            'both inactive and actuator-group members',
        ):
            assembly._validate_compatibility()

    def test_profile_revision_mismatch_is_rejected(self):
        selection = json.loads(self.selection_path.read_text(encoding='utf-8'))
        selection['profiles']['mounted_effector']['expected_revision'] = 'wrong'
        with tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
            path = Path(temporary) / 'selection.json'
            path.write_text(json.dumps(selection), encoding='utf-8')
            with self.assertRaisesRegex(AssemblyConfigurationError, 'revision'):
                RobotAssemblyConfiguration.load(path, self.workspace)

    def test_provider_profile_path_cannot_escape_provider_root(self):
        selection = json.loads(self.selection_path.read_text(encoding='utf-8'))
        selection['profiles']['mounted_effector']['relative_path'] = '../../README.md'
        with tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
            path = Path(temporary) / 'selection.json'
            path.write_text(json.dumps(selection), encoding='utf-8')
            with self.assertRaisesRegex(AssemblyConfigurationError, 'outside'):
                RobotAssemblyConfiguration.load(path, self.workspace)

    def test_effector_inertial_reference_frame_must_match_attachment(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        assembly.profiles['mounted_effector']['inertial']['reference_frame'] = 'link6'
        with self.assertRaisesRegex(AssemblyConfigurationError, 'inertial reference frame'):
            assembly._validate_compatibility()

    def test_collision_point_frames_must_match_selected_kinematic_chain(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        assembly.profiles['collision_geometry']['polyline_point_frames'][-1] = 'end_link'
        with self.assertRaisesRegex(AssemblyConfigurationError, 'kinematic chain'):
            assembly._validate_compatibility()

    def test_collision_primitives_require_known_frames_and_positive_dimensions(self):
        assembly = RobotAssemblyConfiguration.load(
            self.selection_path,
            self.workspace,
        )
        primitive = {
            'primitive_id': 'knife-body',
            'frame_id': 'missing-frame',
            'transform': {
                'translation_m': [0.0, 0.0, 0.0],
                'rpy_rad': [0.0, 0.0, 0.0],
            },
            'shape': {'type': 'BOX', 'size_m': [0.1, 0.02, 0.0]},
            'qualification': 'DEVELOPMENT',
        }
        assembly.profiles['collision_geometry']['frame_primitives'] = [primitive]
        with self.assertRaisesRegex(AssemblyConfigurationError, 'unknown frame'):
            assembly._validate_compatibility()

        primitive['frame_id'] = 'rebot_arm_tool'
        with self.assertRaisesRegex(AssemblyConfigurationError, 'positive dimensions'):
            assembly._validate_compatibility()


class CoreTests(unittest.TestCase):
    def test_hardware_io_telemetry_counts_feedback_and_command_frames(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        backend.read()
        backend.send_impedance(0, 0.0, 0.0, 120.0, 8.0, 0.0)
        diagnostics = backend.diagnostics()
        self.assertEqual(diagnostics["read_cycles"], 1)
        self.assertEqual(diagnostics["command_frames"], 1)
        self.assertGreaterEqual(diagnostics["command_frames_per_s"], 0.0)

    def test_feedback_deadline_retains_control_loop_jitter_margin(self):
        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        diagnostics = backend.diagnostics()
        self.assertEqual(diagnostics["feedback_cycle_timeout_ms"], 40.0)
        self.assertEqual(diagnostics["feedback_rerequest_interval_ms"], 4.0)

    def test_explicit_hot_recovery_requalifies_fresh_feedback_and_fences_lease(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        lease = controller.acquire_lease("integrated", 6000)
        controller.state = ProviderState.FAULTED
        controller.health = "FAULTED"
        controller.last_error = "fresh feedback generation did not advance"

        result = controller.recover_fault_to_gravity_float()

        self.assertTrue(result["recovered"])
        self.assertEqual(
            controller.state,
            ProviderState.SAFE_HOLD_GRAVITY_FLOAT,
        )
        self.assertEqual(controller.health, "HEALTHY")
        self.assertIsNone(controller.last_error)
        self.assertIsNone(controller.lease)
        self.assertGreater(controller.fencing_generation, lease.fencing_generation)
        self.assertEqual(controller.last_lease_event["event"], "REVOKED")
        recovery = controller.snapshot()["loop"]["fault_recovery"]
        self.assertEqual(recovery["attempt_count"], 1)
        self.assertEqual(recovery["success_count"], 1)
        self.assertEqual(recovery["failure_count"], 0)
        self.assertGreaterEqual(backend.command_frame_count, 7)

    def test_explicit_hot_recovery_rejects_old_feedback(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.feedback.observed_monotonic -= 1.0
        controller.state = ProviderState.FAULTED
        controller.health = "FAULTED"

        result = controller.recover_fault_to_gravity_float()

        self.assertFalse(result["recovered"])
        self.assertEqual(result["status"], "waiting_for_fresh_feedback")
        self.assertEqual(controller.state, ProviderState.FAULTED)
        self.assertEqual(controller.health, "FAULTED")
        recovery = controller.snapshot()["loop"]["fault_recovery"]
        self.assertEqual(recovery["attempt_count"], 1)
        self.assertEqual(recovery["success_count"], 0)
        self.assertEqual(recovery["failure_count"], 1)

    def test_explicit_hot_recovery_clears_fault_health_after_safe_float_fallback(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        controller.health = "FAULTED"
        controller.last_error = "transient control output failed"

        result = controller.recover_fault_to_gravity_float()

        self.assertTrue(result["recovered"])
        self.assertEqual(
            result["status"],
            "fresh_feedback_requalified_into_gravity_float",
        )
        self.assertEqual(controller.state, ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
        self.assertEqual(controller.health, "HEALTHY")
        self.assertIsNone(controller.last_error)

    def test_motorbridge_retries_one_windows_semaphore_timeout(self):
        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        calls = 0

        def transient_operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "dm-serial write failed: The semaphore timeout period has expired. (os error 121)"
                )

        backend._with_transient_retry(transient_operation)
        diagnostics = backend.diagnostics()
        self.assertEqual(calls, 2)
        self.assertEqual(diagnostics["transient_retry_attempts"], 1)
        self.assertEqual(diagnostics["transient_retry_recoveries"], 1)
        self.assertEqual(diagnostics["transient_retry_failures"], 0)

    def test_motorbridge_persistent_semaphore_timeout_still_fails(self):
        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        calls = 0

        def persistent_failure():
            nonlocal calls
            calls += 1
            raise RuntimeError(
                "dm-serial write failed: The semaphore timeout period has expired. (os error 121)"
            )

        with self.assertRaisesRegex(RuntimeError, "semaphore timeout"):
            backend._with_transient_retry(persistent_failure)
        diagnostics = backend.diagnostics()
        self.assertEqual(calls, 3)
        self.assertEqual(diagnostics["transient_retry_attempts"], 2)
        self.assertEqual(diagnostics["transient_retry_failures"], 1)

    def test_motorbridge_retries_one_device_command_error(self):
        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        calls = 0

        def transient_operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "dm-serial write failed: The device does not recognize the command. (os error 22)"
                )

        backend._with_transient_retry(transient_operation)
        self.assertEqual(calls, 2)
        self.assertEqual(backend.diagnostics()["transient_retry_recoveries"], 1)

    def test_motorbridge_retries_missing_mode_register_confirmation(self):
        class ModeMotor:
            def __init__(self):
                self.calls = 0

            def ensure_mode(self, mode, timeout_ms):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("register 10 not received within 100ms")

        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        motor = ModeMotor()
        backend.motors = [motor]
        backend.mode_enum = type("Modes", (), {"POS_VEL": object()})
        backend.active_modes = [None]
        backend._ensure_mode(0, "POSITION_VELOCITY_LIMITED")
        self.assertEqual(motor.calls, 2)
        self.assertEqual(backend.active_modes, ["POS_VEL"])
        self.assertEqual(backend.diagnostics()["transient_retry_recoveries"], 1)

    def test_motorbridge_duplicates_first_mit_frame_after_mode_switch(self):
        class FakeMotor:
            def __init__(self):
                self.mode_switches = 0
                self.mit_frames = 0

            def ensure_mode(self, mode, timeout_ms):
                self.mode_switches += 1

            def send_mit(self, *values):
                self.mit_frames += 1

        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        motor = FakeMotor()
        backend.motors = [motor]
        backend.mode_enum = type("Modes", (), {"MIT": object()})
        backend.active_modes = ["POS_VEL"]
        backend.send_impedance(0, 0.0, 0.0, 120.0, 8.0, 0.0)
        self.assertEqual(motor.mode_switches, 1)
        self.assertEqual(motor.mit_frames, 2)
        backend.send_impedance(0, 0.0, 0.0, 120.0, 8.0, 0.0)
        self.assertEqual(motor.mit_frames, 3)

    def test_motorbridge_sends_mit_support_before_waiting_for_mode_confirmation(self):
        class RegisterMotor:
            def __init__(self):
                self.events = []

            def write_register_u32(self, register, value):
                self.events.append(("write", register, value))

            def send_mit(self, *values):
                self.events.append(("mit",))

            def get_register_u32(self, register, timeout_ms):
                self.events.append(("read", register, timeout_ms))
                return 1

        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        motor = RegisterMotor()
        backend.motors = [motor]
        backend.mode_enum = type("Modes", (), {"MIT": 1})
        backend.active_modes = ["POS_VEL"]
        backend.send_impedance(0, 0.0, 0.0, 120.0, 8.0, 0.0)
        self.assertEqual(
            motor.events,
            [
                ("write", 10, 1),
                ("mit",),
                ("read", 10, 1000),
                ("mit",),
            ],
        )

    def test_motorbridge_initial_mit_startup_uses_reliable_ensure_mode_path(self):
        class StartupMotor:
            def __init__(self):
                self.ensure_calls = 0
                self.mit_frames = 0

            def ensure_mode(self, mode, timeout_ms):
                self.ensure_calls += 1

            def send_mit(self, *values):
                self.mit_frames += 1

            def write_register_u32(self, register, value):
                raise AssertionError("startup must not use the early handoff bridge")

            def get_register_u32(self, register, timeout_ms):
                raise AssertionError("startup must not use direct register confirmation")

        backend = MotorBridgeBackend(self.config, "COM3", 921600)
        motor = StartupMotor()
        backend.motors = [motor]
        backend.mode_enum = type("Modes", (), {"MIT": 1})
        backend.active_modes = [None]
        backend.send_impedance(0, 0.0, 0.0, 120.0, 8.0, 0.0)
        self.assertEqual(motor.ensure_calls, 1)
        self.assertEqual(motor.mit_frames, 2)

    def test_newest_command_envelope_replaces_the_previous_pending_envelope(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        lease = controller.acquire_lease("overwrite-test", 1000)
        first = CommandEnvelope(
            "first",
            lease.lease_id,
            lease.fencing_generation,
            {0: JointCommand("POSITION_VELOCITY_LIMITED", {"position_rad": 0.1, "velocity_limit_rad_s": 0.2})},
            time.monotonic() + 0.5,
        )
        second = CommandEnvelope(
            "second",
            lease.lease_id,
            lease.fencing_generation,
            {0: JointCommand("POSITION_VELOCITY_LIMITED", {"position_rad": 0.2, "velocity_limit_rad_s": 0.2})},
            time.monotonic() + 0.5,
        )

        controller.submit(first)
        controller.submit(second)

        self.assertEqual(controller.pending.command_id, "second")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["command_ingress"]["semantics"], "LATEST_VALID_ENVELOPE_REPLACES_PREVIOUS")
        self.assertEqual(snapshot["command_ingress"]["last_replaced_command_id"], "first")
        self.assertEqual(snapshot["command_ingress"]["replacement_count"], 1)

    def test_motor_side_endpoint_keepalive_is_throttled_below_control_rate(self):
        class RecordingBackend(SimulationBackend):
            def __init__(self, configuration, gravity_function):
                super().__init__(configuration, gravity_function)
                self.pos_vel_calls = 0

            def send_position_velocity(self, index, position, velocity_limit):
                self.pos_vel_calls += 1
                super().send_position_velocity(index, position, velocity_limit)

        backend = RecordingBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.active_command_modes = ["IMPEDANCE"] * 7
        controller.active_command_modes[6] = "POSITION_VELOCITY_LIMITED"
        command = JointCommand(
            "POSITION_VELOCITY_LIMITED",
            self.config.validate_joint_command(
                6,
                "POSITION_VELOCITY_LIMITED",
                {
                    "position_rad": float(self.config.home_positions[6]),
                    "velocity_limit_rad_s": 0.2,
                },
            ),
        )
        envelope = CommandEnvelope(
            "latched",
            "unused",
            0,
            {6: command},
            time.monotonic() + 1.0,
        )

        controller._apply_pending_locked(envelope)
        controller._apply_pending_locked(envelope)
        self.assertEqual(backend.pos_vel_calls, 1)
        self.assertEqual(controller.latched_endpoint_frames_suppressed, 1)
        time.sleep(controller.endpoint_keepalive_period_s + 0.01)
        controller._apply_pending_locked(envelope)
        self.assertEqual(backend.pos_vel_calls, 2)

    def test_gripper_position_effort_uses_provider_owned_rate_ramp(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.active_command_modes = ["IMPEDANCE"] * 7
        controller.active_command_modes[6] = "POSITION_EFFORT_LIMITED"
        start = float(controller.feedback.positions_rad[6])
        velocity_limit = 0.1
        target = start - 0.5
        command = JointCommand(
            "POSITION_EFFORT_LIMITED",
            self.config.validate_joint_command(
                6,
                "POSITION_EFFORT_LIMITED",
                {
                    "position_rad": target,
                    "velocity_limit_rad_s": velocity_limit,
                    "torque_limit_ratio": 0.1,
                },
            ),
        )
        envelope = CommandEnvelope(
            "gripper-rate-ramp",
            "unused",
            0,
            {6: command},
            time.monotonic() + 1.0,
        )

        controller._apply_pending_locked(envelope)
        first = float(backend.commands[6]["position"])
        controller._apply_pending_locked(envelope)
        second = float(backend.commands[6]["position"])

        step = velocity_limit * controller.period
        self.assertAlmostEqual(first, start - step, delta=1e-12)
        self.assertAlmostEqual(second, start - 2.0 * step, delta=1e-12)
        self.assertAlmostEqual(
            float(backend.commands[6]["vlim"]),
            velocity_limit
            * controller.gripper_force_position_native_velocity_scale,
            delta=1e-12,
        )
        telemetry = controller.snapshot()["latched_endpoint_output"]
        self.assertEqual(
            telemetry["gripper_position_effort_rate_policy"],
            "PROVIDER_RAMP_NATIVE_TRANSLATION_AND_MEASURED_SPEED_GUARD",
        )
        guard = telemetry["gripper_measured_speed_guard"]
        self.assertFalse(guard["active"])
        self.assertEqual(guard["trip_count"], 0)
        self.assertAlmostEqual(guard["requested_limit_rad_s"], velocity_limit)
        self.assertAlmostEqual(
            guard["native_limit_rad_s"],
            velocity_limit
            * controller.gripper_force_position_native_velocity_scale,
        )

    def test_gripper_position_effort_brakes_on_measured_speed_and_resumes_with_hysteresis(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.active_command_modes = ["IMPEDANCE"] * 7
        controller.active_command_modes[6] = "POSITION_EFFORT_LIMITED"
        velocity_limit = 0.2
        start = float(controller.feedback.positions_rad[6])
        target = start - 0.5
        command = JointCommand(
            "POSITION_EFFORT_LIMITED",
            self.config.validate_joint_command(
                6,
                "POSITION_EFFORT_LIMITED",
                {
                    "position_rad": target,
                    "velocity_limit_rad_s": velocity_limit,
                    "torque_limit_ratio": 0.1,
                },
            ),
        )
        envelope = CommandEnvelope(
            "gripper-measured-speed-guard",
            "unused",
            0,
            {6: command},
            time.monotonic() + 1.0,
        )

        controller.feedback.velocities_rad_s[6] = velocity_limit + 0.01
        controller._apply_pending_locked(envelope)
        hold_position = float(controller.feedback.positions_rad[6])
        self.assertTrue(controller.gripper_velocity_guard_active)
        self.assertAlmostEqual(
            float(backend.commands[6]["position"]),
            hold_position,
            delta=1e-12,
        )

        controller.feedback.positions_rad[6] = hold_position - 0.02
        controller.feedback.velocities_rad_s[6] = velocity_limit * 0.9
        controller._apply_pending_locked(envelope)
        self.assertTrue(controller.gripper_velocity_guard_active)
        self.assertAlmostEqual(
            float(backend.commands[6]["position"]),
            hold_position,
            delta=1e-12,
        )

        controller.feedback.velocities_rad_s[6] = velocity_limit * 0.7
        controller._apply_pending_locked(envelope)
        expected = (
            float(controller.feedback.positions_rad[6])
            - velocity_limit * controller.period
        )
        self.assertFalse(controller.gripper_velocity_guard_active)
        self.assertAlmostEqual(
            float(backend.commands[6]["position"]),
            expected,
            delta=1e-12,
        )
        guard = controller.snapshot()["latched_endpoint_output"][
            "gripper_measured_speed_guard"
        ]
        self.assertEqual(guard["trip_count"], 1)
        self.assertGreaterEqual(
            guard["peak_measured_rad_s"],
            velocity_limit + 0.01,
        )

    def test_arm_position_effort_retains_latched_native_endpoint(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.active_command_modes = ["IMPEDANCE"] * 7
        controller.active_command_modes[3] = "POSITION_EFFORT_LIMITED"
        target = float(controller.feedback.positions_rad[3]) + 0.1
        command = JointCommand(
            "POSITION_EFFORT_LIMITED",
            self.config.validate_joint_command(
                3,
                "POSITION_EFFORT_LIMITED",
                {
                    "position_rad": target,
                    "velocity_limit_rad_s": 0.1,
                    "torque_limit_ratio": 0.1,
                },
            ),
        )
        envelope = CommandEnvelope(
            "arm-native-endpoint",
            "unused",
            0,
            {3: command},
            time.monotonic() + 1.0,
        )

        controller._apply_pending_locked(envelope)

        self.assertAlmostEqual(
            float(backend.commands[3]["position"]),
            target,
            delta=1e-12,
        )

    def test_snapshot_returns_cached_fault_telemetry_while_motor_io_holds_control_lock(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        backend.connect()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        fresh = controller.snapshot()
        self.assertFalse(fresh["snapshot_delivery"]["cached"])

        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_control_lock():
            with controller.lock:
                lock_held.set()
                release_lock.wait(1.0)

        worker = threading.Thread(target=hold_control_lock)
        worker.start()
        self.assertTrue(lock_held.wait(0.5))
        started = time.monotonic()
        cached = controller.snapshot()
        elapsed = time.monotonic() - started
        release_lock.set()
        worker.join(1.0)

        self.assertLess(elapsed, 0.1)
        self.assertTrue(cached["snapshot_delivery"]["cached"])
        self.assertEqual(cached["positions_rad"], fresh["positions_rad"])
        self.assertEqual(cached["observed_at_us"], fresh["observed_at_us"])
        self.assertGreater(cached["feedback_age_ms"], fresh["feedback_age_ms"])
        self.assertEqual(
            cached["feedback_timing"]["timestamp_semantics"],
            "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE",
        )

    def setUp(self):
        self.config=configuration(); self.kin=RebotKinematics(self.config.model); self.dyn=RebotDynamics(self.config,self.kin)

    def test_disjoint_arm_and_gripper_group_leases_merge_without_cross_scope(self):
        backend = SimulationBackend(
            self.config,
            self.dyn.calibrated_gravity_torque,
        )
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        controller.configure_resource_groups(
            "robot_arm.primary",
            [
                {
                    "resource_id": "robot_arm.primary/arm",
                    "joint_names": [
                        "joint1", "joint2", "joint3",
                        "joint4", "joint5", "joint6",
                    ],
                },
                {
                    "resource_id": "robot_arm.primary/gripper",
                    "joint_names": ["gripper"],
                },
            ],
        )
        arm = controller.acquire_group_lease(
            "robot_arm.primary/arm", "integrated-free-space", 1000
        )
        gripper = controller.acquire_group_lease(
            "robot_arm.primary/gripper", "grip-controller", 1000
        )
        with self.assertRaisesRegex(LeasePermissionError, "already leased"):
            controller.acquire_group_lease(
                "robot_arm.primary/arm", "contender", 1000
            )
        with self.assertRaisesRegex(LeasePermissionError, "root control conflicts"):
            controller.acquire_lease("legacy-root", 1000)

        arm_command = CommandEnvelope(
            "arm-command",
            arm.lease_id,
            arm.fencing_generation,
            {
                0: JointCommand(
                    "IMPEDANCE",
                    {
                        "position_rad": 0.1,
                        "velocity_rad_s": 0.0,
                        "target_rate_limit_rad_s": 0.25,
                        "kp": 120.0,
                        "kd": 8.0,
                        "feedforward_torque_nm": 0.0,
                    },
                )
            },
            time.monotonic() + 0.5,
            resource_id="robot_arm.primary/arm",
        )
        grip_command = CommandEnvelope(
            "grip-command",
            gripper.lease_id,
            gripper.fencing_generation,
            {
                6: JointCommand(
                    "POSITION_EFFORT_LIMITED",
                    {
                        "position_rad": float(self.config.home_positions[6]),
                        "velocity_limit_rad_s": 0.1,
                        "torque_limit_ratio": 0.1,
                    },
                )
            },
            time.monotonic() + 0.5,
            resource_id="robot_arm.primary/gripper",
        )
        controller.submit_group(arm_command)
        controller.submit_group(grip_command)
        for _ in range(4):
            controller._tick(time.monotonic())

        self.assertEqual(controller.active_command_modes[0], "IMPEDANCE")
        self.assertEqual(
            controller.active_command_modes[6],
            "POSITION_EFFORT_LIMITED",
        )
        snapshot = controller.snapshot()
        resources = {
            item["resource_id"] for item in snapshot["resource_group_leases"]
        }
        self.assertEqual(
            resources,
            {"robot_arm.primary/arm", "robot_arm.primary/gripper"},
        )

        wrong_scope = CommandEnvelope(
            "wrong-scope",
            arm.lease_id,
            arm.fencing_generation,
            {6: grip_command.commands[6]},
            time.monotonic() + 0.5,
            resource_id="robot_arm.primary/arm",
        )
        with self.assertRaisesRegex(LeasePermissionError, "may command only"):
            controller.submit_group(wrong_scope)

        self.assertTrue(
            controller.release_group_lease(
                "robot_arm.primary/arm",
                arm.lease_id,
                arm.fencing_generation,
            )
        )
        controller._tick(time.monotonic())
        self.assertEqual(controller.active_command_modes[0], "IMPEDANCE")
        self.assertEqual(
            controller.active_command_modes[6],
            "POSITION_EFFORT_LIMITED",
        )
        controller.revoke_lease("test group safety preemption")
        self.assertFalse(controller.group_leases)
        self.assertFalse(controller.group_pending)

    def test_group_release_with_only_idle_sibling_immediately_recaptures_safely(self):
        backend = SimulationBackend(
            self.config,
            self.dyn.calibrated_gravity_torque,
        )
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        controller.configure_resource_groups(
            "robot_arm.primary",
            [
                {
                    "resource_id": "robot_arm.primary/arm",
                    "joint_names": [
                        "joint1", "joint2", "joint3",
                        "joint4", "joint5", "joint6",
                    ],
                },
                {
                    "resource_id": "robot_arm.primary/gripper",
                    "joint_names": ["gripper"],
                },
            ],
        )
        arm = controller.acquire_group_lease(
            "robot_arm.primary/arm", "integrated-free-space", 1000
        )
        controller.acquire_group_lease(
            "robot_arm.primary/gripper", "idle-grip-controller", 1000
        )
        controller.submit_group(
            CommandEnvelope(
                "arm-command-before-release",
                arm.lease_id,
                arm.fencing_generation,
                {
                    0: JointCommand(
                        "IMPEDANCE",
                        {
                            "position_rad": 0.1,
                            "velocity_rad_s": 0.0,
                            "target_rate_limit_rad_s": 0.25,
                            "kp": 120.0,
                            "kd": 8.0,
                            "feedforward_torque_nm": 0.0,
                        },
                    )
                },
                time.monotonic() + 0.5,
                resource_id="robot_arm.primary/arm",
            )
        )
        controller._tick(time.monotonic())
        self.assertEqual(controller.state, ProviderState.CALIBRATION_MANUAL)

        siblings_remain = controller.release_group_lease(
            "robot_arm.primary/arm",
            arm.lease_id,
            arm.fencing_generation,
        )

        self.assertTrue(siblings_remain)
        self.assertEqual(controller.state, ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
        self.assertNotIn("robot_arm.primary/arm", controller.group_pending)
        self.assertIn("robot_arm.primary/gripper", controller.group_leases)

    def test_expired_group_renewal_leaves_authority_for_control_loop_fallback(self):
        backend = SimulationBackend(
            self.config,
            self.dyn.calibrated_gravity_torque,
        )
        backend.connect()
        backend.enable()
        controller = ArmController(self.config, backend, self.dyn)
        controller.feedback = backend.read()
        controller.state = ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        controller.configure_resource_groups(
            "robot_arm.primary",
            [
                {
                    "resource_id": "robot_arm.primary/arm",
                    "joint_names": [
                        "joint1", "joint2", "joint3",
                        "joint4", "joint5", "joint6",
                    ],
                },
                {
                    "resource_id": "robot_arm.primary/gripper",
                    "joint_names": ["gripper"],
                },
            ],
        )
        arm = controller.acquire_group_lease(
            "robot_arm.primary/arm", "integrated-free-space", 1000
        )
        arm.expires_monotonic = time.monotonic() - 0.001

        with self.assertRaisesRegex(LeasePermissionError, "expired"):
            controller.renew_group_lease(
                "robot_arm.primary/arm",
                arm.lease_id,
                arm.fencing_generation,
                1000,
            )

        self.assertIn("robot_arm.primary/arm", controller.group_leases)
        controller._tick(time.monotonic())
        self.assertNotIn("robot_arm.primary/arm", controller.group_leases)
        self.assertEqual(controller.state, ProviderState.SAFE_HOLD_GRAVITY_FLOAT)

    def test_forward_kinematics(self):
        frames=self.kin.frames(self.config.home_positions)
        self.assertEqual(len(frames),8); self.assertTrue(np.all(np.isfinite(frames[-1])))

    def test_gravity_is_finite(self):
        torque=self.dyn.nominal_gravity_torque(self.config.home_positions)
        self.assertEqual(torque.shape,(7,)); self.assertTrue(np.all(np.isfinite(torque))); self.assertEqual(torque[6],0.0)

    def test_limit_rejection(self):
        with self.assertRaises(ValueError): self.config.validate_joint_command(0,'POSITION_VELOCITY_LIMITED',{'position_rad':100,'velocity_limit_rad_s':0.1})

    def test_physical_pos_vel_caps_match_configured_motor_vmax(self):
        expected = [5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0]
        self.assertEqual(
            [float(value) for value in self.config.model["control"]["physical_test_pos_vel_cap_rad_s"]],
            expected,
        )
        for index, cap in enumerate(expected):
            configured_vmax = float(
                self.config.model["joints"][index]["motor_limits"]["configured_vmax_rad_s"]
            )
            self.assertLessEqual(cap, configured_vmax)
            accepted = self.config.validate_joint_command(
                index,
                "POSITION_VELOCITY_LIMITED",
                {"position_rad": float(self.config.home_positions[index]), "velocity_limit_rad_s": cap},
            )
            self.assertEqual(float(accepted["velocity_limit_rad_s"]), cap)
            with self.assertRaisesRegex(ValueError, "velocity limit"):
                self.config.validate_joint_command(
                    index,
                    "POSITION_VELOCITY_LIMITED",
                    {
                        "position_rad": float(self.config.home_positions[index]),
                        "velocity_limit_rad_s": cap + 0.01,
                    },
                )

    def test_physical_pos_tor_caps_reach_arm_motor_peak_but_not_gripper(self):
        expected = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.2]
        self.assertEqual(
            [float(value) for value in self.config.model["control"]["physical_test_pos_tor_ratio_cap"]],
            expected,
        )
        command = self.config.validate_joint_command(
            3,
            "POSITION_EFFORT_LIMITED",
            {
                "position_rad": 0.0,
                "velocity_limit_rad_s": 0.1,
                "torque_limit_ratio": 0.95,
            },
        )
        self.assertEqual(command["torque_limit_ratio"], 0.95)

        with self.assertRaisesRegex(ValueError, "MIT target rate"):
            self.config.validate_joint_command(
                1,
                "IMPEDANCE",
                {
                    "position_rad": -0.1,
                    "velocity_rad_s": 0.0,
                    "target_rate_limit_rad_s": 0.36,
                    "kp": 120.0,
                    "kd": 1.0,
                    "feedforward_torque_nm": 0.0,
                },
            )

    def test_four_simulation_modes(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque); backend.connect(); backend.enable()
        backend.send_impedance(0,0.1,0,2,1,0); backend.send_position_velocity(1,-0.1,0.1); backend.send_velocity(2,-0.1); backend.send_force_position(3,0.1,0.1,0.1)
        time.sleep(0.03); feedback=backend.read(); self.assertTrue(np.all(np.isfinite(feedback.positions_rad)))

    def test_motorbridge_collects_all_seven_feedback_frames(self):
        class FakeState:
            def __init__(self,index):
                self.pos=float(index); self.vel=0.0; self.torq=0.0; self.status_code=0

        class FakeMotor:
            def __init__(self,index):
                self.index=index; self.state=None; self.generation=0; self.requested=False; self.request_count=0
            def request_feedback(self):
                self.requested=True; self.request_count+=1
            def get_state(self):
                return self.state
            def get_state_sample(self):
                return None if self.state is None else (self.state,self.generation,0)

        class FakeController:
            instance=None
            def __init__(self):
                self.motors=[]; self.poll_count=0
                FakeController.instance=self
            @classmethod
            def from_dm_serial(cls,port,baudrate):
                return cls()
            def add_damiao_motor(self,motor_id,feedback_id,motor_model):
                motor=FakeMotor(len(self.motors)); self.motors.append(motor); return motor
            def poll_feedback_once(self):
                self.poll_count+=1
                for motor in self.motors:
                    if motor.requested:
                        motor.state=FakeState(motor.index); motor.generation+=1; motor.requested=False
            def disable_all(self):
                pass
            def shutdown(self):
                pass
            def close(self):
                pass

        fake_module=types.SimpleNamespace(Controller=FakeController,Mode=types.SimpleNamespace())
        with patch.dict(sys.modules,{'motorbridge':fake_module}):
            backend=MotorBridgeBackend(self.config,'COM3',921600); backend.connect(); feedback=backend.read()
        self.assertEqual(FakeController.instance.poll_count,1)
        self.assertEqual(feedback.positions_rad.shape,(7,))
        self.assertTrue(np.all(np.isfinite(feedback.positions_rad)))

    def test_motorbridge_does_not_register_or_poll_inactive_gripper(self):
        class FakeState:
            def __init__(self, index):
                self.pos = float(index)
                self.vel = 0.0
                self.torq = 0.0
                self.status_code = 0

        class FakeMotor:
            def __init__(self, index):
                self.index = index
                self.state = None
                self.generation = 0
                self.requested = False

            def request_feedback(self):
                self.requested = True

            def get_state_sample(self):
                if self.state is None:
                    return None
                return self.state, self.generation, 0

        class FakeController:
            instance = None

            def __init__(self):
                self.motors = []
                FakeController.instance = self

            @classmethod
            def from_dm_serial(cls, port, baudrate):
                return cls()

            def add_damiao_motor(self, motor_id, feedback_id, motor_model):
                motor = FakeMotor(len(self.motors))
                self.motors.append(motor)
                return motor

            def poll_feedback_once(self):
                for motor in self.motors:
                    if motor.requested:
                        motor.state = FakeState(motor.index)
                        motor.generation += 1
                        motor.requested = False

            def disable_all(self):
                pass

            def shutdown(self):
                pass

            def close(self):
                pass

        fake_module = types.SimpleNamespace(
            Controller=FakeController,
            Mode=types.SimpleNamespace(),
        )
        with patch.dict(sys.modules, {'motorbridge': fake_module}):
            backend = MotorBridgeBackend(self.config, 'COM3', 921600)
            backend.configure_inactive_joints({6})
            backend.connect()
            feedback = backend.read()

        self.assertEqual(len(FakeController.instance.motors), 6)
        self.assertEqual(feedback.positions_rad.shape, (7,))
        self.assertEqual(feedback.status_codes[6], 'INACTIVE_NOT_INSTALLED')
        self.assertEqual(feedback.feedback_generations[6], 0)
        self.assertEqual(feedback.per_joint_observed_at_us[6], 0)
        self.assertAlmostEqual(
            feedback.positions_rad[6],
            self.config.home_positions[6],
        )
        self.assertEqual(
            backend.diagnostics()['inactive_joint_names'],
            ['gripper'],
        )

    def test_motorbridge_rerequests_dropped_gripper_feedback(self):
        class FakeState:
            def __init__(self,index):
                self.pos=float(index); self.vel=0.0; self.torq=0.0; self.status_code=0

        class FakeMotor:
            def __init__(self,index):
                self.index=index; self.state=None; self.generation=0; self.requested=False; self.request_count=0
            def request_feedback(self):
                self.requested=True; self.request_count+=1
            def get_state(self):
                return self.state
            def get_state_sample(self):
                return None if self.state is None else (self.state,self.generation,0)

        class FakeController:
            instance=None
            def __init__(self):
                self.motors=[]; self.poll_count=0
                FakeController.instance=self
            @classmethod
            def from_dm_serial(cls,port,baudrate):
                return cls()
            def add_damiao_motor(self,motor_id,feedback_id,motor_model):
                motor=FakeMotor(len(self.motors)); self.motors.append(motor); return motor
            def poll_feedback_once(self):
                self.poll_count+=1
                for motor in self.motors:
                    if not motor.requested:
                        continue
                    # Simulate the first request to motor 7 being lost.
                    if motor.index==6 and motor.request_count==1:
                        motor.requested=False
                        continue
                    motor.state=FakeState(motor.index); motor.generation+=1; motor.requested=False
            def disable_all(self):
                pass
            def shutdown(self):
                pass
            def close(self):
                pass

        fake_module=types.SimpleNamespace(Controller=FakeController,Mode=types.SimpleNamespace())
        with patch.dict(sys.modules,{'motorbridge':fake_module}):
            backend=MotorBridgeBackend(self.config,'COM3',921600); backend.connect(); feedback=backend.read()
        self.assertGreaterEqual(FakeController.instance.poll_count,2)
        self.assertEqual(FakeController.instance.motors[6].request_count,2)
        self.assertTrue(np.all(np.isfinite(feedback.positions_rad)))

    def test_motorbridge_rejects_cached_state_when_a_later_reply_is_dropped(self):
        class FakeState:
            def __init__(self,index):
                self.pos=float(index); self.vel=0.0; self.torq=0.0; self.status_code=0

        class FakeMotor:
            def __init__(self,index):
                self.index=index; self.state=None; self.generation=0; self.requested=False
            def request_feedback(self):
                self.requested=True
            def get_state_sample(self):
                return None if self.state is None else (self.state,self.generation,0)

        class FakeController:
            def __init__(self):
                self.motors=[]; self.completed_cycles=0
            @classmethod
            def from_dm_serial(cls,port,baudrate):
                return cls()
            def add_damiao_motor(self,motor_id,feedback_id,motor_model):
                motor=FakeMotor(len(self.motors)); self.motors.append(motor); return motor
            def poll_feedback_once(self):
                for motor in self.motors:
                    if not motor.requested:
                        continue
                    if self.completed_cycles>=1 and motor.index==6:
                        motor.requested=False
                        continue
                    motor.state=FakeState(motor.index); motor.generation+=1; motor.requested=False
                if all(motor.generation>=1 for motor in self.motors):
                    self.completed_cycles=1
            def disable_all(self):
                pass
            def shutdown(self):
                pass
            def close(self):
                pass

        fake_module=types.SimpleNamespace(Controller=FakeController,Mode=types.SimpleNamespace())
        with patch.dict(sys.modules,{'motorbridge':fake_module}):
            backend=MotorBridgeBackend(self.config,'COM3',921600); backend.connect()
            first=backend.read()
            with self.assertRaisesRegex(RuntimeError,'gripper'):
                backend.read()
        self.assertEqual(first.feedback_generations,[1]*7)
        self.assertEqual(backend.feedback_stale_rejection_count,1)

    def test_controller_does_not_retain_feedback_after_a_failed_fresh_read(self):
        class FailingBackend(SimulationBackend):
            def __init__(self, config, gravity):
                super().__init__(config, gravity)
                self.fail = False
            def read(self):
                if self.fail:
                    raise RuntimeError("fresh feedback unavailable")
                return super().read()

        backend=FailingBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect()
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        backend.fail=True

        with self.assertRaisesRegex(RuntimeError,"fresh feedback unavailable"):
            controller._tick(time.monotonic())

        self.assertIsNone(controller.feedback)

    def test_command_expiry_enters_gravity_float(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque); controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        lease=controller.acquire_lease('test',1000)
        controller.submit(CommandEnvelope('c',lease.lease_id,lease.fencing_generation,{0:JointCommand('IMPEDANCE',{'position_rad':0.1,'velocity_rad_s':0,'target_rate_limit_rad_s':0.25,'kp':120,'kd':1,'feedforward_torque_nm':0})},time.monotonic()+0.05))
        time.sleep(0.15); self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT); controller.close(force=True)

    def test_graceful_safe_home_simulation(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque); controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        self.assertTrue(controller.graceful_stop()); self.assertEqual(controller.state,ProviderState.DISCONNECTED); controller.stop_event.set()

    def test_safe_home_from_moved_position(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque); controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        lease=controller.acquire_lease('move',2000)
        controller.submit(CommandEnvelope('move',lease.lease_id,lease.fencing_generation,{3:JointCommand('POSITION_VELOCITY_LIMITED',{'position_rad':0.2,'velocity_limit_rad_s':0.3})},time.monotonic()+1.5))
        time.sleep(0.8); self.assertTrue(controller.safe_home(8.0)); self.assertLess(abs(controller.snapshot()['positions_rad'][3]),0.08); controller.close(force=True)

    def test_safe_home_caller_can_only_reduce_configured_velocity_limit(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        self.assertTrue(controller.safe_home(1.0,max_velocity_rad_s=0.125))
        result=controller.snapshot()['last_safe_home_result']
        self.assertAlmostEqual(result['requested_max_velocity_rad_s'],0.125)
        self.assertAlmostEqual(result['effective_max_velocity_rad_s'],0.125)
        configured=float(self.config.model['control']['safe_home_max_velocity_rad_s'])
        self.assertTrue(controller.safe_home(1.0,max_velocity_rad_s=configured+1.0))
        result=controller.snapshot()['last_safe_home_result']
        self.assertAlmostEqual(result['effective_max_velocity_rad_s'],configured)
        with self.assertRaisesRegex(ValueError,'max_velocity_rad_s'):
            controller.safe_home(1.0,max_velocity_rad_s=0.0)
        controller.close(force=True)

    def test_safe_home_fences_active_lease_before_first_supported_frame(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        lease=controller.acquire_lease('integrated-gripper-latch',6000)
        observed_leases=[]
        original=controller._send_supported_mit_target_locked
        def wrapped(target,kp,kd):
            with controller.ingress_lock:
                observed_leases.append(controller.lease)
            return original(target,kp,kd)
        controller._send_supported_mit_target_locked=wrapped
        self.assertTrue(controller.safe_home(8.0))
        self.assertTrue(observed_leases)
        self.assertTrue(all(active is None for active in observed_leases))
        self.assertEqual(controller.last_lease_event['event'],'REVOKED')
        stale=CommandEnvelope(
            'stale-gripper-keepalive',
            lease.lease_id,
            lease.fencing_generation,
            {6:JointCommand('IMPEDANCE',{
                'position_rad':float(self.config.home_positions[6]),
                'velocity_rad_s':0.0,
                'target_rate_limit_rad_s':0.25,
                'kp':8.0,
                'kd':1.0,
                'feedforward_torque_nm':0.0,
            })},
            time.monotonic()+0.5,
        )
        with self.assertRaises(LeasePermissionError):
            controller.submit(stale)
        controller.close(force=True)

    def test_safe_home_state_rejects_new_operational_commands(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.state=ProviderState.SAFE_HOME
        with self.assertRaises(LeasePermissionError) as raised:
            controller.acquire_lease('late-integrated-command',6000)
        self.assertEqual(raised.exception.error_code,'OPERATIONAL_CONTROL_BLOCKED')
        self.assertIsNone(controller.lease)

    def test_safe_home_excludes_concurrent_operational_reacquisition(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        with backend.lock:
            backend.position[5]=-0.35
            backend.velocity[5]=0.0
        self.assertTrue(
            wait_for(
                lambda: abs(controller.snapshot()['positions_rad'][5]+0.35)<0.03,
                1.0,
            )
        )
        result=[]
        worker=threading.Thread(target=lambda:result.append(controller.safe_home(8.0)))
        worker.start()
        self.assertTrue(
            wait_for(
                lambda: controller.snapshot()['last_safe_home_result']['active'],
                1.0,
            )
        )
        with self.assertRaises(LeasePermissionError) as raised:
            controller.acquire_lease('integrated-background-reacquire',6000)
        self.assertEqual(raised.exception.error_code,'OPERATIONAL_CONTROL_BLOCKED')
        worker.join(10.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result,[True])
        state=controller.snapshot()
        self.assertIsNone(state['lease'])
        self.assertIsNone(state['lease_diagnostics']['operational_control_block_reason'])
        self.assertTrue(state['last_safe_home_result']['success'])
        controller.close(force=True)

    def test_gravity_float_explicitly_cancels_safe_home_writer(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        with backend.lock:
            backend.position[5]=-0.35
            backend.velocity[5]=0.0
        self.assertTrue(
            wait_for(
                lambda: abs(controller.snapshot()['positions_rad'][5]+0.35)<0.03,
                1.0,
            )
        )
        result=[]
        worker=threading.Thread(target=lambda:result.append(controller.safe_home(8.0)))
        worker.start()
        self.assertTrue(
            wait_for(
                lambda: controller.snapshot()['last_safe_home_result']['active'],
                1.0,
            )
        )
        controller.request_gravity_float('explicit safety cancellation')
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result,[False])
        state=controller.snapshot()
        self.assertEqual(state['provider_state'],ProviderState.SAFE_HOLD_GRAVITY_FLOAT.value)
        self.assertIn('cancelled',state['last_safe_home_result']['reason'])
        self.assertIsNone(state['lease_diagnostics']['operational_control_block_reason'])
        controller.close(force=True)

    def test_service_lease_acquisition_cannot_replace_active_safe_home(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.state=ProviderState.SAFE_HOME
        with tempfile.TemporaryDirectory() as temp:
            service=ArmProviderService(
                self.config,controller,self.kin,Path(temp)/'calibration.json',
                '127.0.0.1',0,None,None,True,True
            )
            service.manager_registered=True
            with patch.object(
                controller,
                'request_gravity_float',
                side_effect=AssertionError('rejected acquisition must not change control state'),
            ):
                with self.assertRaises(LeasePermissionError) as raised:
                    service.acquire_operational_lease(
                        {'holder':'integrated-background-reacquire','duration_ms':6000}
                    )
            self.assertEqual(raised.exception.error_code,'OPERATIONAL_CONTROL_BLOCKED')
            self.assertEqual(controller.state,ProviderState.SAFE_HOME)

    def test_warm_release_and_hot_reconnect(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque); controller=ArmController(self.config,backend,self.dyn); controller.start()
        self.assertTrue(controller.enter_warm()); self.assertEqual(controller.state,ProviderState.DISCONNECTED)
        controller.start(); self.assertEqual(controller.state,ProviderState.READ_ONLY); controller.close(force=True)

    def test_calibration_regression(self):
        rng=np.random.default_rng(5); samples=[]
        for t in np.linspace(0,20,2000):
            q=.45*np.sin(.7*t)+.22*np.sin(1.9*t+.3)
            qd=.315*np.cos(.7*t)+.418*np.cos(1.9*t+.3)
            qdd=-.2205*np.sin(.7*t)-.7942*np.sin(1.9*t+.3)
            g=2*np.sin(q)+.2*np.cos(.5*q)+.1
            tau=1.08*g+.14*qdd+.09*np.tanh(qd/.03)+.04*qd-.03+rng.normal(0,.01)
            samples.append({'time_s':float(t),'position_rad':float(q),'velocity_rad_s':float(qd),'acceleration_rad_s2':float(qdd),'nominal_gravity_nm':float(g),'measured_torque_nm':float(tau)})
        fit=robust_fit(samples,True); self.assertTrue(fit.accepted); self.assertAlmostEqual(fit.gravity_scale,1.08,delta=.03); self.assertAlmostEqual(fit.effective_inertia,.14,delta=.03)


    def test_official_and_unity_control_defaults(self):
        expected=[(120.0,8.0,60.0),(120.0,8.0,60.0),(120.0,8.0,60.0),
                  (18.0,2.0,20.0),(18.0,2.0,20.0),(18.0,2.0,20.0),(8.0,1.0,20.0)]
        for joint,values in zip(self.config.model["joints"],expected):
            kp,kd,effort=values
            self.assertEqual(float(joint["default_test"]["kp"]),kp)
            self.assertEqual(float(joint["default_test"]["kd"]),kd)
            self.assertEqual(float(joint["provider_test_caps"]["mit_tracking_effort_limit_nm"]),effort)

    def test_gravity_matches_potential_energy_gradient_convention(self):
        q=self.config.home_positions.copy(); q[1]=-0.4; q[2]=-0.6; q[3]=0.2
        torque=self.dyn.nominal_gravity_torque(q)
        delta=np.zeros(7); delta[1]=1e-5; delta[2]=-2e-5
        measured=self.dyn.potential_energy(q+delta)-self.dyn.potential_energy(q)
        predicted=float(torque@delta)
        self.assertAlmostEqual(measured,predicted,delta=1e-7)

    def test_gravity_float_uses_official_arm_gains_and_measured_position(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        time.sleep(0.05)
        with backend.lock:
            for index in range(6):
                self.assertEqual(backend.commands[index]["mode"],"IMPEDANCE")
                self.assertEqual(float(backend.commands[index]["kp"]),[120.0,120.0,120.0,18.0,18.0,18.0][index])
                self.assertEqual(float(backend.commands[index]["kd"]),1.0)
                self.assertAlmostEqual(float(backend.commands[index]["position"]),float(backend.position[index]),delta=0.01)
            self.assertEqual(float(backend.commands[6]["kp"]),8.0)
            self.assertEqual(float(backend.commands[6]["kd"]),1.0)
        controller.close(force=True)

    def test_mit_tracking_effort_limiter_uses_unity_limits(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        with controller.lock:
            controller.feedback.velocities_rad_s[0]=-100.0
            lease=controller.acquire_lease('limiter',1000)
            envelope=CommandEnvelope('limited',lease.lease_id,lease.fencing_generation,
                {0:JointCommand('IMPEDANCE',{'position_rad':float(controller.feedback.positions_rad[0]),'velocity_rad_s':0.0,'kp':120.0,'kd':8.0,'feedforward_torque_nm':0.0})},
                time.monotonic()+1.0)
            controller.submit(envelope); controller._apply_pending_locked(envelope)
            sent=backend.commands[0]
            sent_effort=120.0*(sent['position']-controller.feedback.positions_rad[0])+8.0*(sent['velocity']-controller.feedback.velocities_rad_s[0])
            self.assertLessEqual(abs(float(sent_effort)),60.000001)
        controller.close(force=True)


    def test_gravity_float_applies_full_gravity_immediately(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        backend.position[1]=-0.8; backend.position[2]=-0.7
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        expected=self.dyn.calibrated_gravity_torque(controller.feedback.positions_rad)
        controller.request_gravity_float('deadman release')
        with backend.lock:
            for index in range(6):
                self.assertAlmostEqual(float(backend.commands[index]['torque']),float(expected[index]),delta=1e-9)
        self.assertEqual(controller.last_float_reason,'deadman release')

    def test_uncommanded_gripper_recaptures_measured_pose(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        backend.position[6]=-0.3374147415161133
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        envelope=CommandEnvelope('arm-only','unused',0,{0:JointCommand('IMPEDANCE',self.config.validate_joint_command(0,'IMPEDANCE',{
            'position_rad':0.0,'velocity_rad_s':0.0,'target_rate_limit_rad_s':0.08,
            'kp':120.0,'kd':8.0,'feedforward_torque_nm':0.0}))},time.monotonic()+1.0)
        controller._apply_pending_locked(envelope)
        self.assertEqual(backend.commands[6]['mode'],'IMPEDANCE')
        self.assertAlmostEqual(float(backend.commands[6]['position']),-0.3374147415161133,delta=1e-12)
        self.assertAlmostEqual(float(controller.hold_reference[6]),-0.3374147415161133,delta=1e-12)

    def test_mit_moving_target_accumulates_instead_of_resetting_from_feedback(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        controller.active_command_modes[0]='IMPEDANCE'
        controller.mit_moving_target[0]=float(controller.feedback.positions_rad[0])
        command=JointCommand('IMPEDANCE',self.config.validate_joint_command(0,'IMPEDANCE',{
            'position_rad':0.5,'velocity_rad_s':0.0,'target_rate_limit_rad_s':0.25,
            'kp':120.0,'kd':8.0,'feedforward_torque_nm':0.0}))
        envelope=CommandEnvelope('mit','unused',0,{0:command},time.monotonic()+1.0)
        controller._apply_pending_locked(envelope)
        first=float(backend.commands[0]['position'])
        controller._apply_pending_locked(envelope)
        second=float(backend.commands[0]['position'])
        self.assertGreater(second,first)
        self.assertAlmostEqual(controller.mit_moving_target[0],2.0*0.25*controller.period,delta=1e-9)

    def test_active_lease_rejects_takeover_and_scoped_release_fences_stale_owner(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        first=controller.acquire_lease('first',2000)
        controller.submit(CommandEnvelope('old',first.lease_id,first.fencing_generation,{0:JointCommand('IMPEDANCE',{
            'position_rad':0.1,'velocity_rad_s':0.0,'target_rate_limit_rad_s':0.25,'kp':120.0,'kd':8.0,'feedforward_torque_nm':0.0})},time.monotonic()+1.0))
        with self.assertRaises(LeasePermissionError) as conflict:
            controller.acquire_lease('second',2000)
        self.assertEqual(conflict.exception.error_code,'ACTIVE_LEASE_CONFLICT')
        self.assertEqual(controller.lease.lease_id,first.lease_id)
        controller.release_lease(first.lease_id,first.fencing_generation,fallback_to_float=True)
        self.assertIsNone(controller.lease)
        self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
        second=controller.acquire_lease('second',2000)
        with self.assertRaises(LeasePermissionError) as stale:
            controller.release_lease(first.lease_id,first.fencing_generation,fallback_to_float=True)
        self.assertEqual(controller.lease.lease_id,second.lease_id)
        self.assertEqual(stale.exception.error_code,'STALE_LEASE')
        controller.close(force=True)

    def test_concurrent_lease_acquisition_allows_exactly_one_owner(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn); controller.start(); controller.enable()
        gate=threading.Barrier(8)
        successes=[]; failures=[]
        result_lock=threading.Lock()

        def acquire(index):
            gate.wait()
            try:
                lease=controller.acquire_lease(f'contender-{index}',2000)
                with result_lock: successes.append(lease)
            except LeasePermissionError as error:
                with result_lock: failures.append(error.error_code)

        threads=[threading.Thread(target=acquire,args=(index,)) for index in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=2.0)
        self.assertEqual(len(successes),1)
        self.assertEqual(len(failures),7)
        self.assertTrue(all(code=='ACTIVE_LEASE_CONFLICT' for code in failures))
        self.assertEqual(controller.lease.lease_id,successes[0].lease_id)
        controller.close(force=True)

    def test_calibration_enabled_service_starts_in_gravity_float(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        with tempfile.TemporaryDirectory() as temp:
            service=ArmProviderService(self.config,controller,self.kin,Path(temp)/'calibration.json','127.0.0.1',0,None,None,True,True)
            service.start()
            self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
            service.shutdown(False)

    def test_default_service_start_is_gravity_float_without_calibration_permission(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        with tempfile.TemporaryDirectory() as temp:
            service=ArmProviderService(self.config,controller,self.kin,Path(temp)/'calibration.json','127.0.0.1',0,None,None,False,True)
            service.start()
            self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
            self.assertFalse(service.health()['allow_hardware_calibration'])
            service.shutdown(False)

    def test_explicit_read_only_service_does_not_enable_motors(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        with tempfile.TemporaryDirectory() as temp:
            service=ArmProviderService(self.config,controller,self.kin,Path(temp)/'calibration.json','127.0.0.1',0,None,None,False,True,True)
            service.start()
            self.assertEqual(controller.state,ProviderState.READ_ONLY)
            self.assertTrue(service.health()['read_only'])
            with self.assertRaises(PermissionError):
                service.acquire_lease({'holder':'blocked'})
            service.shutdown(False)

    def test_disarm_to_float_revokes_lease_without_safe_home(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        with tempfile.TemporaryDirectory() as temp:
            service=ArmProviderService(self.config,controller,self.kin,Path(temp)/'calibration.json','127.0.0.1',0,None,None,True,True)
            service.start()
            controller.acquire_lease('test',1000)
            with patch.object(controller,'safe_home',side_effect=AssertionError('safe-home must not run')):
                service.disarm_to_float('test interrupt')
            self.assertIsNone(controller.lease)
            self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
            self.assertEqual(controller.last_float_reason,'test interrupt')
            service.shutdown(False)

    def test_no_lease_forces_position_hold_back_to_gravity_float(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        with controller.lock:
            controller.state=ProviderState.CALIBRATION_POSITION_HOLD
        time.sleep(0.05)
        self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
        self.assertEqual(controller.last_float_reason,'no active lease')
        controller.close(force=True)

    def test_scheduler_skips_missed_slots_without_catchup_burst(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        adjusted=controller._account_schedule_lateness(100.084,100.0)
        expected_missed=int((100.084-100.0)//controller.period)
        self.assertEqual(controller.deadline_overrun_events,1)
        self.assertEqual(controller.missed_deadlines,expected_missed)
        self.assertAlmostEqual(adjusted,100.0+expected_missed*controller.period,places=9)
        next_scheduled=adjusted+controller.period
        controller._account_schedule_lateness(100.084,next_scheduled)
        self.assertEqual(controller.deadline_overrun_events,1)
        self.assertEqual(controller.missed_deadlines,expected_missed)

    def test_manager_entry_enables_calibration_float_and_gui_has_reset(self):
        entry=json.loads((ROOT/'config_templates'/'provider_entry.json').read_text())
        self.assertIn('--allow-hardware-calibration',entry['args'])
        launcher=(ROOT/'scripts'/'run_provider.ps1').read_text()
        self.assertIn('[switch]$ReadOnly',launcher)
        self.assertIn('--read-only',launcher)
        html=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'index.html').read_text()
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        self.assertIn('reset-manual-defaults',html)
        self.assertIn('reset-auto-defaults',html)
        self.assertIn('snapRowToMeasured',script)
        self.assertIn('target_rate_limit_rad_s',script)
        self.assertIn('timeout_ms:150',script)
        self.assertIn('manual-motion-status',html)


    def test_release_freezes_motor_side_target_refreshes_support_then_switches_one_joint(self):
        class RecordingBackend(SimulationBackend):
            def __init__(self,configuration,gravity_function):
                super().__init__(configuration,gravity_function); self.calls=[]
            def send_impedance(self,index,position,velocity,kp,kd,torque):
                self.calls.append(("mit",index,float(position))); super().send_impedance(index,position,velocity,kp,kd,torque)
            def send_position_velocity(self,index,position,velocity_limit):
                self.calls.append(("pos_vel",index,float(position))); super().send_position_velocity(index,position,velocity_limit)

        backend=RecordingBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable(); backend.position[4]=0.21
        controller=ArmController(self.config,backend,self.dyn); controller.feedback=backend.read()
        controller.active_command_modes[4]="POSITION_VELOCITY_LIMITED"
        backend.calls.clear(); controller.request_gravity_float("slider released")
        self.assertEqual(backend.calls[0][0:2],("pos_vel",4))
        self.assertAlmostEqual(backend.calls[0][2],0.21,delta=1e-9)
        self.assertEqual(backend.calls[-2][0:2],("pos_vel",4))
        self.assertEqual(backend.calls[-1][0:2],("mit",4))
        self.assertTrue(all(call[0]=="mit" for call in backend.calls[1:-2]))
        self.assertEqual(controller.state,ProviderState.SAFE_HOLD_GRAVITY_FLOAT)

    def test_float_reports_pending_modes_and_returns_one_motor_side_joint_per_tick(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        controller.active_command_modes=["IMPEDANCE"]*7
        controller.active_command_modes[1]="POSITION_VELOCITY_LIMITED"
        controller.active_command_modes[4]="POSITION_EFFORT_LIMITED"
        controller.request_gravity_float("multi-mode release")
        self.assertEqual(controller.snapshot()["float_transition_pending_joint_indices"],[4])
        controller._apply_gravity_float_locked(time.monotonic())
        self.assertEqual(controller.snapshot()["float_transition_pending_joint_indices"],[])

    def test_motor_side_mode_transition_changes_one_joint_per_tick_before_endpoint_motion(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        backend.position=self.config.home_positions.copy()
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        controller.active_command_modes=["IMPEDANCE"]*7
        targets=np.asarray([
            0.5*(joint.operational_min+joint.operational_max)
            for joint in self.config.joints[:6]
        ])
        commands={
            index:JointCommand("POSITION_VELOCITY_LIMITED",self.config.validate_joint_command(
                index,
                "POSITION_VELOCITY_LIMITED",
                {"position_rad":float(targets[index]),"velocity_limit_rad_s":0.1},
            ))
            for index in range(6)
        }
        envelope=CommandEnvelope("pos-vel","unused",0,commands,time.monotonic()+1.0)
        for expected_steps in range(1,7):
            controller._apply_pending_locked(envelope)
            self.assertEqual(controller.mode_transition_step_count,expected_steps)
            self.assertEqual(
                controller.active_command_modes[:6].count("POSITION_VELOCITY_LIMITED"),
                expected_steps,
            )
            self.assertAlmostEqual(float(backend.commands[0]["position"]),float(controller.feedback.positions_rad[0]),delta=1e-12)
        controller._apply_pending_locked(envelope)
        for index in range(6):
            self.assertAlmostEqual(float(backend.commands[index]["position"]),float(targets[index]),delta=1e-12)

    def test_manager_request_reads_nested_payload_reason(self):
        source=(ROOT/'python'/'rebot_arm_dm_provider'/'service.py').read_text()
        self.assertIn('request_payload = body.get("payload", {})',source)
        self.assertIn('request_payload.get("reason"',source)

    def test_manual_slider_preserves_native_pointer_drag(self):
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        html=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'index.html').read_text()
        self.assertNotIn('event.preventDefault()',script)
        self.assertNotIn("event.code==='Space'",script)
        self.assertIn("window.addEventListener(eventName",script)
        self.assertIn("state.activePointerId=event.pointerId",script)
        self.assertIn('setManualSlidersEnabled(true)',script)
        self.assertIn('disabled aria-label=',script)
        self.assertIn('id="manual-motion-status"',html)

    def test_manual_slider_release_remains_gravity_float_only(self):
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        release=script[script.index('async function releaseDeadman'):script.index('function snapRowToMeasured')]
        self.assertIn("api('/api/gravity-float','POST')",release)
        self.assertIn('snapRowToMeasured(row)',release)
        self.assertNotIn("api('/api/safe-home'",release)

    def test_collision_guard(self):
        guard=CalibrationCollisionGuard.load(self.kin,str(ROOT/'config_templates'/'calibration_collision_model.json'))
        result=guard.check(self.config.home_positions,table_height_m=-0.25,table_clearance_m=.01)
        self.assertTrue(np.isfinite(result.minimum_clearance_m))

    def test_rich_calibration_fit_recovers_phase_and_asymmetric_friction(self):
        rng=np.random.default_rng(11); samples=[]
        gravity_scale=1.12; phase_offset=0.035; inertia=0.18; fc_pos=0.12; fc_neg=0.08; viscous=0.05; bias=-0.025
        for t in np.linspace(0,30,3000):
            q=.32*np.sin(.75*t)+.12*np.sin(2.1*t+.4)
            qd=.24*np.cos(.75*t)+.252*np.cos(2.1*t+.4)
            qdd=-.18*np.sin(.75*t)-.5292*np.sin(2.1*t+.4)
            g=2.2*np.sin(q)+.3*np.cos(.4*q)
            dg=2.2*np.cos(q)-.12*np.sin(.4*q)
            sign=np.tanh(qd/.03)
            friction=fc_pos*max(sign,0.0)+fc_neg*min(sign,0.0)+viscous*qd
            tau=gravity_scale*g+(gravity_scale*phase_offset)*dg+inertia*qdd+friction+bias+rng.normal(0,.012)
            samples.append({'time_s':float(t),'position_rad':float(q),'velocity_rad_s':float(qd),'acceleration_rad_s2':float(qdd),'nominal_gravity_nm':float(g),'gravity_gradient_nm_per_rad':float(dg),'measured_torque_nm':float(tau)})
        fit=robust_fit(samples,True)
        self.assertTrue(fit.accepted)
        self.assertAlmostEqual(fit.gravity_scale,gravity_scale,delta=.04)
        self.assertAlmostEqual(fit.gravity_phase_offset_rad,phase_offset,delta=.015)
        self.assertAlmostEqual(fit.effective_inertia,inertia,delta=.04)
        self.assertAlmostEqual(fit.coulomb_friction_positive_nm,fc_pos,delta=.03)
        self.assertAlmostEqual(fit.coulomb_friction_negative_nm,fc_neg,delta=.03)

    def test_automatic_calibration_uses_captured_pose_and_minimal_friction_excitation(self):
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        html=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'index.html').read_text()
        service=(ROOT/'python'/'rebot_arm_dm_provider'/'service.py').read_text()
        self.assertIn('autoAnchorPose',script)
        self.assertIn('anchor_positions_rad:anchor',script)
        self.assertIn('Capture current gravity-float pose',html)
        self.assertIn('value="0.16"',html)
        self.assertIn('value="0.32"',html)
        self.assertNotIn('include-inertia',html)
        self.assertNotIn('id="passes"',html)
        self.assertIn('save-raw-samples',html)
        auto=script[script.index('async function startAutomatic'):script.index('function renderFit')]
        self.assertNotIn("api('/api/safe-home'",auto)
        self.assertIn('FRICTION_TWO_SPEED_POS_VEL_V2',service)
        self.assertIn('friction_slow_positive',service)
        self.assertIn('friction_slow_negative',service)
        self.assertIn('friction_fast_positive',service)
        self.assertIn('friction_fast_negative',service)
        self.assertNotIn('static_hold_',service)


    def test_two_parameter_friction_fit_cancels_factory_gravity(self):
        rng=np.random.default_rng(21)
        samples=[]
        coulomb=0.11
        viscous=0.075
        for label,speed in (("slow",0.16),("fast",0.32)):
            for direction in (1.0,-1.0):
                phase=f"friction_{label}_{'positive' if direction>0 else 'negative'}"
                for q in np.linspace(-0.35,0.35,180):
                    velocity=direction*speed*(0.94+0.03*np.cos(3*q))
                    gravity=1.8*np.sin(q)+0.22*np.cos(2*q)
                    bias=0.06
                    torque=gravity+bias+direction*coulomb+viscous*velocity+rng.normal(0,0.006)
                    samples.append({
                        'time_s':float(len(samples)*0.05),
                        'phase':phase,
                        'commanded_speed_rad_s':speed,
                        'position_rad':float(q),
                        'velocity_rad_s':float(velocity),
                        'measured_torque_nm':float(torque),
                        'nominal_gravity_nm':float(gravity),
                    })
        fit=fit_two_parameter_friction(samples)
        self.assertTrue(fit.accepted)
        self.assertTrue(fit.factory_gravity_retained)
        self.assertAlmostEqual(fit.coulomb_friction_nm,coulomb,delta=0.02)
        self.assertAlmostEqual(fit.viscous_friction_nm_per_rad_s,viscous,delta=0.03)

    def test_experiment_keeps_speed_limited_hold_before_fit_and_file_work(self):
        service=(ROOT/'python'/'rebot_arm_dm_provider'/'service.py').read_text()
        hold=service.index('self.controller.request_position_hold(\n                "friction calibration motion completed"')
        fit=service.index('fit=fit_two_parameter_friction(samples)')
        write=service.index('self.recorder.write_result(path,result)')
        self.assertLess(hold,fit)
        self.assertLess(hold,write)
        experiment=service[service.index('def _run_experiment_locked'):service.index('def _check_experiment_state')]
        self.assertNotIn('request_gravity_float',experiment)
        self.assertIn('"control_mode":"POSITION_VELOCITY_LIMITED"',experiment)

    def test_routine_polling_logs_are_suppressed(self):
        gui=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_gui.py').read_text()
        provider=(ROOT/'python'/'rebot_arm_dm_provider'/'service.py').read_text()
        self.assertIn("'/api/state'",gui)
        self.assertIn("'/api/collision/check'",gui)
        self.assertIn("'/v1/arm/state'",provider)
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        self.assertIn("slice(-16)",script)

    def test_friction_apply_does_not_modify_factory_gravity_fields(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        original=self.config.calibration_by_name['joint4'].copy()
        with tempfile.TemporaryDirectory() as temp:
            calibration_path=Path(temp)/'arm_calibration.json'
            calibration_path.write_text(json.dumps(self.config.calibration))
            service=ArmProviderService(self.config,controller,self.kin,calibration_path,'127.0.0.1',0,None,None,True,True)
            result=service.apply_fit({'joint_index':3,'fit':{
                'accepted':True,'friction_identifiable':True,'coulomb_friction_nm':0.12,
                'viscous_friction_nm_per_rad_s':0.04,'factory_gravity_retained':True,
                'condition_number':5.0,'pair_count':30,'slow_pair_count':15,'fast_pair_count':15,
                'slow_speed_rad_s':0.16,'fast_speed_rad_s':0.32,
            }})
        updated=self.config.calibration_by_name['joint4']
        self.assertEqual(updated.get('gravity_scale'),original.get('gravity_scale'))
        self.assertEqual(updated.get('gravity_phase_offset_rad'),original.get('gravity_phase_offset_rad'))
        self.assertEqual(updated.get('effective_inertia_kg_m2'),original.get('effective_inertia_kg_m2'))
        self.assertAlmostEqual(updated['coulomb_friction_nm'],0.12)
        self.assertAlmostEqual(updated['viscous_friction_nm_per_rad_s'],0.04)
        self.assertTrue(result['factory_gravity_retained'])

    def test_gravity_phase_offset_is_applied_only_to_gravity_model(self):
        q=self.config.home_positions.copy(); q[1]=-0.7; q[2]=-0.5
        before=self.dyn.calibrated_gravity_torque(q)
        self.config.calibration_by_name['joint2']['gravity_phase_offset_rad']=0.05
        after=self.dyn.calibrated_gravity_torque(q)
        self.assertFalse(np.allclose(before,after))
        self.assertTrue(np.allclose(q,self.config.home_positions+np.array([0,-0.7,-0.5,0,0,0,0])))

    def test_load_bearing_mit_rejects_low_kp_but_allows_low_kd(self):
        with self.assertRaisesRegex(ValueError, 'low spring stiffness'):
            self.config.validate_joint_command(0,'IMPEDANCE',{
                'position_rad':0.0,'velocity_rad_s':0.0,'target_rate_limit_rad_s':0.1,
                'kp':2.0,'kd':1.0,'feedforward_torque_nm':0.0})
        accepted=self.config.validate_joint_command(0,'IMPEDANCE',{
            'position_rad':0.0,'velocity_rad_s':0.0,'target_rate_limit_rad_s':0.1,
            'kp':120.0,'kd':0.1,'feedforward_torque_nm':0.0})
        self.assertEqual(accepted['kp'],120.0)
        self.assertEqual(accepted['kd'],0.1)

    def test_safe_float_uses_provided_high_kp_defaults(self):
        expected=[120.0,120.0,120.0,18.0,18.0,18.0,8.0]
        actual=[float(self.config.calibration_by_name[j.name]['safe_float_kp']) for j in self.config.joints]
        self.assertEqual(actual,expected)
        for index,joint in enumerate(self.config.model['joints']):
            self.assertEqual(float(joint['provider_test_caps']['min_kp']),expected[index])

    def test_automatic_gui_does_not_request_float_between_joints(self):
        script=(ROOT/'python'/'rebot_arm_dm_provider'/'calibration_web'/'app.js').read_text()
        auto=script[script.index('async function startAutomatic'):script.index('function renderFit')]
        self.assertNotIn("api('/api/gravity-float'",auto)
        self.assertIn('Speed-limited position hold remains active',auto)


    def test_safe_home_kp_is_never_below_load_bearing_floor(self):
        configured=np.asarray(self.config.model['control']['safe_home_kp'],dtype=float)
        floors=np.asarray([float(j['provider_test_caps']['min_kp']) for j in self.config.model['joints']],dtype=float)
        self.assertTrue(np.all(configured>=floors))

    def test_configuration_rejects_low_safe_home_spring_stiffness(self):
        import copy
        from rebot_arm_dm_provider.models import ConfigurationError
        model=copy.deepcopy(self.config.model)
        calibration=copy.deepcopy(self.config.calibration)
        model['control']['safe_home_kp'][0]=2.0
        with self.assertRaisesRegex(ConfigurationError,'safe_home_kp'):
            ArmConfiguration(model,calibration)

    def test_safe_home_first_frame_captures_current_pose_with_high_kp(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        with backend.lock:
            backend.position[3]=0.22
            backend.velocity[3]=0.0
        time.sleep(0.04)
        starting=np.asarray(controller.snapshot()['positions_rad'],dtype=float)
        records=[]
        original=controller._send_supported_mit_target_locked
        def wrapped(target,kp,kd):
            records.append((np.asarray(target,dtype=float).copy(),np.asarray(kp,dtype=float).copy(),np.asarray(kd,dtype=float).copy()))
            return original(target,kp,kd)
        controller._send_supported_mit_target_locked=wrapped
        self.assertTrue(controller.safe_home(8.0))
        self.assertTrue(records)
        first_target,first_kp,first_kd=records[0]
        self.assertTrue(np.allclose(first_target,starting,atol=0.03))
        floors=np.asarray([float(j['provider_test_caps']['min_kp']) for j in self.config.model['joints']],dtype=float)
        self.assertTrue(np.all(first_kp>=floors))
        self.assertTrue(np.all(first_kd>=0.0))
        controller.close(force=True)

    def test_safe_home_preserves_gripper_angle_instead_of_clamping(self):
        backend=SimulationBackend(self.config,self.dyn.calibrated_gravity_torque)
        backend.connect(); backend.enable()
        preserved=-1.2
        with backend.lock:
            backend.position[6]=preserved
            backend.velocity[6]=0.0
        controller=ArmController(self.config,backend,self.dyn)
        controller.feedback=backend.read()
        controller.state=ProviderState.READ_ONLY
        records=[]
        original=controller._send_supported_mit_target_locked

        def wrapped(target,kp,kd):
            records.append(np.asarray(target,dtype=float).copy())
            return original(target,kp,kd)

        controller._send_supported_mit_target_locked=wrapped
        self.assertTrue(controller.safe_home(1.0))
        self.assertTrue(records)
        self.assertTrue(
            all(abs(float(target[6])-preserved)<1e-12 for target in records)
        )
        result=controller.snapshot()["last_safe_home_result"]
        self.assertEqual(result["gripper_policy"],"PRESERVE_MEASURED_ANGLE")
        self.assertAlmostEqual(result["gripper_target_rad"],preserved,delta=1e-12)

    def test_graceful_stop_sends_supported_frame_immediately_before_disable(self):
        class RecordingBackend(SimulationBackend):
            def __init__(self,configuration,gravity_function):
                super().__init__(configuration,gravity_function); self.events=[]
            def send_impedance(self,index,position,velocity,kp,kd,torque):
                self.events.append(('mit',int(index),float(kp)))
                super().send_impedance(index,position,velocity,kp,kd,torque)
            def disable(self):
                self.events.append(('disable',))
                super().disable()
        backend=RecordingBackend(self.config,self.dyn.calibrated_gravity_torque)
        controller=ArmController(self.config,backend,self.dyn)
        controller.start(); controller.enable()
        with backend.lock:
            backend.position[3]=0.16
        time.sleep(0.03)
        self.assertTrue(controller.graceful_stop())
        disable_index=max(i for i,event in enumerate(backend.events) if event[0]=='disable')
        self.assertGreater(disable_index,0)
        self.assertEqual(backend.events[disable_index-1][0],'mit')
        recent=[event for event in backend.events[max(0,disable_index-14):disable_index] if event[0]=='mit']
        self.assertGreaterEqual(len(recent),7)
        floors=[120.0,120.0,120.0,18.0,18.0,18.0,8.0]
        by_joint={}
        for _,index,kp in recent:
            by_joint[index]=kp
        self.assertEqual(set(by_joint),set(range(7)))
        for index,floor in enumerate(floors):
            self.assertGreaterEqual(by_joint[index],floor)

    def test_handover_docs_mark_kp_not_kd_as_hard_safety_floor(self):
        safety=(ROOT/'docs'/'SAFETY.md').read_text()
        architecture=(ROOT/'docs'/'ARCHITECTURE.md').read_text()
        self.assertIn('Low `kp` is prohibited',safety)
        self.assertIn('`kd` is velocity damping',safety)
        self.assertIn('low spring stiffness is forbidden',architecture)

if __name__=='__main__': unittest.main()

class FastIngressTests(unittest.TestCase):
    def test_lease_renew_and_command_submission_do_not_wait_for_hardware_lock(self):
        config=configuration(); kin=RebotKinematics(config.model); dyn=RebotDynamics(config,kin)
        backend=SimulationBackend(config,dyn.calibrated_gravity_torque)
        controller=ArmController(config,backend,dyn)
        controller.state=ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        lease=controller.acquire_lease('fast-ingress',6000)
        entered=threading.Event(); release=threading.Event()
        def hold_control_lock():
            with controller.lock:
                entered.set(); release.wait(2.0)
        worker=threading.Thread(target=hold_control_lock,daemon=True); worker.start(); self.assertTrue(entered.wait(1.0))
        started=time.monotonic(); controller.renew_lease(lease.lease_id,lease.fencing_generation,6000); renew_elapsed=time.monotonic()-started
        envelope=CommandEnvelope('fast',lease.lease_id,lease.fencing_generation,{0:JointCommand('POSITION_VELOCITY_LIMITED',{'position_rad':0.0,'velocity_limit_rad_s':0.1})},time.monotonic()+0.5)
        started=time.monotonic(); controller.submit(envelope); submit_elapsed=time.monotonic()-started
        release.set(); worker.join(1.0)
        self.assertLess(renew_elapsed,0.1)
        self.assertLess(submit_elapsed,0.1)
        with controller.ingress_lock:
            self.assertIs(controller.pending,envelope)

class PlatformIsolationTests(unittest.TestCase):
    def test_explicit_acquisition_timestamp_is_published_and_recorded(self):
        from rebot_arm_dm_provider.fabric import PlatformPublisher

        class RecordingHttp:
            def __init__(self):
                self.payload = None
            def post(self, url, payload):
                self.payload = payload
                return {"accepted": True}

        publisher = PlatformPublisher(
            'robot_arm.rebot_dm',
            'instance',
            'boot',
            None,
            'http://fabric',
        )
        publisher.http = RecordingHttp()
        publisher.publish(
            'robot_arm.joint_state',
            'physical_agent.robot_arm_joint_state',
            {'positions_rad': [0.0] * 7},
            'rebot_arm_base',
            'calibration',
            observed_at_us=123456,
        )

        self.assertEqual(publisher.http.payload['observed_at_us'], 123456)
        self.assertEqual(
            publisher.output_status('robot_arm.joint_state')['observed_at_us'],
            123456,
        )

    def test_batch_boolean_acceptance_remains_compatible(self):
        from rebot_arm_dm_provider.fabric import PlatformPublisher

        class BooleanAcceptanceHttp:
            def post(self, url, payload):
                return {'accepted': True}

        publisher = PlatformPublisher(
            'robot_arm.rebot_dm',
            'instance',
            'boot',
            None,
            'http://fabric',
        )
        publisher.http = BooleanAcceptanceHttp()
        observations = [
            publisher.observation(
                f'robot_arm.transforms.local.{index}',
                'physical_agent.transform',
                {'translation_m': [0.0, 0.0, 0.0]},
                'rebot_arm_base',
                'calibration',
                observed_at_us=123456,
            )
            for index in range(2)
        ]

        publisher.publish_batch(observations, success_key='robot_arm.transforms.local')

        self.assertEqual(
            publisher.output_status('robot_arm.transforms.local')['observed_at_us'],
            123456,
        )

    def test_slow_manager_request_does_not_block_transform_publication(self):
        config=configuration(); kin=RebotKinematics(config.model); dyn=RebotDynamics(config,kin)
        backend=SimulationBackend(config,dyn.calibrated_gravity_torque)
        controller=ArmController(config,backend,dyn)
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        service=ArmProviderService(
            config,
            controller,
            kin,
            Path(temporary.name)/'calibration.json',
            '127.0.0.1',
            0,
            'http://manager',
            'http://fabric',
            False,
            True,
        )
        manager_entered=threading.Event(); manager_release=threading.Event()
        transform_published=threading.Event()

        class BlockingManagerHttp:
            def post(self, url, payload):
                if url.startswith('http://manager'):
                    manager_entered.set(); manager_release.wait(2.0); return {}
                if url.endswith('/v1/observations/batch'):
                    transform_published.set()
                    return {'accepted': len(payload['observations'])}
                return {'accepted': True}
            def get(self, url):
                return {'inhibited': False, 'owners': []}

        service.publisher.http=BlockingManagerHttp()
        try:
            service.start()
            self.assertTrue(manager_entered.wait(1.0))
            self.assertTrue(transform_published.wait(1.0))
        finally:
            manager_release.set()
            service.shutdown(False)

    def test_manager_failure_does_not_disable_fabric_publication(self):
        from rebot_arm_dm_provider.fabric import PlatformPublisher

        class SelectiveHttp:
            def __init__(self):
                self.urls = []

            def post(self, url, payload):
                self.urls.append(url)
                if ':7001/' in url:
                    raise RuntimeError('manager unavailable')
                return {}

        publisher = PlatformPublisher(
            'robot_arm.rebot_dm',
            'instance',
            'boot',
            'http://127.0.0.1:7001',
            'http://127.0.0.1:7002',
        )
        publisher.http = SelectiveHttp()
        with self.assertRaisesRegex(RuntimeError, 'manager unavailable'):
            publisher.heartbeat({'state': 'SAFE_HOLD_GRAVITY_FLOAT'}, 'http://127.0.0.1:8791')
        publisher.publish(
            'robot_arm.joint_state',
            'physical_agent.robot_arm_joint_state',
            {'positions_rad': [0.0] * 7},
            'rebot_arm_base',
            'calibration',
        )
        self.assertTrue(any(':7002/v1/observations' in url for url in publisher.http.urls))
        self.assertIsNotNone(publisher.errors()['manager'])
        self.assertIsNone(publisher.errors()['fabric'])

    def test_fabric_failure_does_not_disable_manager_heartbeat(self):
        from rebot_arm_dm_provider.fabric import PlatformPublisher

        class SelectiveHttp:
            def __init__(self):
                self.urls = []

            def post(self, url, payload):
                self.urls.append(url)
                if ':7002/' in url:
                    raise RuntimeError('fabric unavailable')
                return {}

        publisher = PlatformPublisher(
            'robot_arm.rebot_dm',
            'instance',
            'boot',
            'http://127.0.0.1:7001',
            'http://127.0.0.1:7002',
        )
        publisher.http = SelectiveHttp()
        with self.assertRaisesRegex(RuntimeError, 'fabric unavailable'):
            publisher.publish(
                'robot_arm.joint_state',
                'physical_agent.robot_arm_joint_state',
                {'positions_rad': [0.0] * 7},
                'rebot_arm_base',
                'calibration',
            )
        publisher.heartbeat({'state': 'SAFE_HOLD_GRAVITY_FLOAT'}, 'http://127.0.0.1:8791')
        self.assertTrue(any(':7001/v1/providers/heartbeat' in url for url in publisher.http.urls))
        self.assertIsNone(publisher.errors()['manager'])
        self.assertIsNotNone(publisher.errors()['fabric'])

class MidbrainAuditTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.kin = RebotKinematics(self.config.model)
        self.dyn = RebotDynamics(self.config, self.kin)

    def make_service(self):
        backend = SimulationBackend(self.config, self.dyn.calibrated_gravity_torque)
        controller = ArmController(self.config, backend, self.dyn)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = ArmProviderService(
            self.config,
            controller,
            self.kin,
            Path(temp.name) / 'calibration.json',
            '127.0.0.1',
            0,
            None,
            None,
            False,
            True,
        )
        service.start()
        self.addCleanup(
            lambda: service.shutdown(False)
            if not service.shutdown_event.is_set()
            else None
        )
        return service, controller

    def test_operational_motion_requires_manager_and_clear_inhibit(self):
        service, controller = self.make_service()
        with self.assertRaisesRegex(PermissionError, 'Manager is not registered'):
            service.acquire_operational_lease(
                {'holder': 'integrated', 'duration_ms': 1000}
            )

        service.manager_registered = True
        lease = service.acquire_operational_lease(
            {'holder': 'integrated', 'duration_ms': 1000}
        )
        self.assertIsNotNone(controller.lease)

        service.motion_inhibited = True
        with self.assertRaisesRegex(PermissionError, 'motion inhibit'):
            service.renew_operational_lease(lease)
        with self.assertRaisesRegex(PermissionError, 'motion inhibit'):
            service.operational_command(
                {**lease, 'commands': [], 'timeout_ms': 100}
            )

    def test_platform_safety_revokes_active_lease_once(self):
        service, controller = self.make_service()
        service.manager_registered = True
        service.acquire_operational_lease(
            {'holder': 'integrated', 'duration_ms': 1000}
        )
        self.assertIsNotNone(controller.lease)
        service.motion_inhibited = True
        service._enforce_platform_safety()
        self.assertIsNone(controller.lease)
        self.assertEqual(controller.state, ProviderState.SAFE_HOLD_GRAVITY_FLOAT)
        first_reason = controller.last_float_reason
        service._enforce_platform_safety()
        self.assertEqual(controller.last_float_reason, first_reason)

    def test_midbrain_capability_readiness_is_reported(self):
        service, _ = self.make_service()
        service.manager_registered = True
        state = service._platform_state()
        self.assertIn('capability_readiness', state)
        self.assertIn('robot.motion.arm.basic', state['capability_readiness'])
        self.assertIn('robot_arm.transforms.local', state['capability_readiness'])
        self.assertFalse(state['capability_readiness']['robot_arm.transforms.local'])
        self.assertEqual(state['audited_midbrain_commit'], 'e226a09')
        self.assertFalse(state['manager_authority_lease_supported'])

        now_us=time.time_ns()//1000
        service.publisher._record_successful_output('robot_arm.joint_state',now_us)
        service.publisher._record_successful_output('robot_arm.transforms.local',now_us)
        ready=service._platform_state()['capability_readiness']
        self.assertTrue(ready['robot_arm.joint_state'])
        self.assertTrue(ready['robot_arm.transforms.local'])

    def test_manager_request_idempotence(self):
        service, controller = self.make_service()
        calls = []
        original = controller.request_gravity_float

        def recorded(reason):
            calls.append(reason)
            return original(reason)

        controller.request_gravity_float = recorded
        body = {
            'action': 'gravity_float',
            'request_id': 'basic-request-1',
            'payload': {'reason': 'test request'},
        }
        first = service.handle_manager_request(body)
        second = service.handle_manager_request(body)
        self.assertEqual(first, second)
        self.assertEqual(calls, ['test request'])

    def test_motion_inhibit_uses_canonical_manager_route(self):
        from rebot_arm_dm_provider.fabric import PlatformPublisher

        class FakeHttp:
            def __init__(self):
                self.urls = []

            def get(self, url):
                self.urls.append(url)
                return {'inhibited': True, 'owners': [{'owner_id': 'test'}]}

        publisher = PlatformPublisher(
            'robot_arm.rebot_dm',
            'instance',
            'boot',
            'http://manager',
            None,
        )
        publisher.http = FakeHttp()
        result = publisher.motion_inhibit()
        self.assertTrue(result['inhibited'])
        self.assertEqual(
            publisher.http.urls,
            ['http://manager/v1/motion/inhibit'],
        )

class PayloadCompensationTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.kin = RebotKinematics(self.config.model)
        self.dyn = RebotDynamics(self.config, self.kin)

    def test_payload_changes_gravity_and_never_commands_gripper_gravity(self):
        q = np.array([0.2, -0.5, -0.6, 0.2, 0.1, -0.1, 0.0], dtype=float)
        arm_only = self.dyn.compensated_gravity_torque(q)
        self.dyn.set_payload(1.0, [0.0, 0.0, 0.15])
        with_payload = self.dyn.compensated_gravity_torque(q)
        self.assertGreater(float(np.linalg.norm(with_payload[:6] - arm_only[:6])), 0.01)
        self.assertEqual(float(with_payload[6]), 0.0)

    def test_payload_update_is_fenced_by_operational_lease(self):
        backend = SimulationBackend(self.config, self.dyn.compensated_gravity_torque)
        controller = ArmController(self.config, backend, self.dyn)
        with self.assertRaises(LeasePermissionError):
            controller.set_payload("missing", 1, 1.0, [0.0, 0.0, 0.1])
        lease = controller.acquire_lease("payload-test", 1000)
        result = controller.set_payload(lease.lease_id, lease.fencing_generation, 1.25, [0.01, 0.0, 0.12])
        self.assertEqual(result["mass_kg"], 1.25)
        with self.assertRaises(LeasePermissionError):
            controller.set_payload(lease.lease_id, lease.fencing_generation + 1, 0.0, [0.0, 0.0, 0.0])

    def test_payload_gravity_is_clipped_to_configured_motor_tmax(self):
        backend = SimulationBackend(self.config, self.dyn.compensated_gravity_torque)
        controller = ArmController(self.config, backend, self.dyn)
        lease = controller.acquire_lease("payload-clip", 1000)
        controller.set_payload(lease.lease_id, lease.fencing_generation, 1000.0, [1.0, 0.0, 1.0])
        q = np.array([0.4, -0.8, -0.9, 0.3, 0.2, -0.2, 0.0], dtype=float)
        with controller.lock:
            gravity = controller._gravity_with_payload_locked(q)
        for index, joint in enumerate(self.config.joints[:6]):
            self.assertLessEqual(abs(float(gravity[index])), float(joint.configured_tmax_nm) + 1e-9)
        self.assertTrue(any(controller.gravity_compensation_clamped[:6]))
