from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from slicing_skill import BLADE_PROFILE_EXTENSION_ID
from slicing_skill.host_adapter import SlicingHostAdapter


def activation(*, activation_id="activation-1"):
    return {
        "activation_id": activation_id,
        "calibration_revision": "calibration-1",
        "world_frame": "world",
        "arm_base_frame": "arm_base",
        "state": "ACTIVE",
        "motion_usable": True,
        "expires_at": None,
        "expires_at_us": None,
        "transforms": {
            "world_from_base": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        },
    }


def effector_profile():
    return {
        "schema": "midbrain.mounted_effector_profile",
        "schema_version": 1,
        "profile_id": "test.blade",
        "profile_revision": "test-1",
        "extensions": {
            BLADE_PROFILE_EXTENSION_ID: {
                "schema": "midbrain.effector_slicing_blade_profiles",
                "schema_version": 1,
                "default_profile_number": 1,
                "profiles": [
                    {
                        "profile_number": 1,
                        "name": "Default",
                        "blade_direction_effector": [1.0, 0.0, 0.0],
                        "slicing_direction_effector": [0.0, 1.0, 0.0],
                        "locked_joint_names": [],
                    }
                ],
            }
        },
    }


def motion_profiles():
    return {
        "schema": "midbrain.slicing_motion_profiles",
        "schema_version": 1,
        "default_profile_number": 1,
        "profiles": [
            {
                "profile_number": 1,
                "name": "Default",
                "blade_load_kgf": 2.0,
                "retract_distance_m": 0.2,
                "delay_after_engage_s": 0.4,
                "slice_wait_speed_m_s": 2.0,
                "delay_after_retract_s": 0.7,
            }
        ],
    }


def agent_arguments():
    return {
        "slice_begin_point_m": [1.0, 2.0, 3.0],
        "blade_direction_world": [0.0, 0.0, -1.0],
        "slicing_direction_world": [1.0, 0.0, 0.0],
        "slice_length_m": 1.0,
    }


def developer_arguments():
    return {
        **agent_arguments(),
        "blade_profile_number": 1,
        "motion_profile_number": 1,
        "blade_direction_effector": [1.0, 0.0, 0.0],
        "slicing_direction_effector": [0.0, 1.0, 0.0],
        "locked_joint_names": ["joint6"],
        "blade_load_kgf": 2.0,
        "retract_distance_m": 0.2,
        "delay_after_engage_s": 0.4,
        "slice_wait_speed_m_s": 2.0,
        "delay_after_retract_s": 0.7,
    }


class FakeManager:
    def __init__(
        self,
        events,
        activations,
        *,
        fail_integrated_warm=False,
        fail_contact_hot=False,
    ):
        self.base_url = "http://manager"
        self.events = events
        self.activations = list(activations)
        self.integrated = None
        self.fail_integrated_warm = fail_integrated_warm
        self.fail_contact_hot = fail_contact_hot

    async def set_hot(self, provider_id):
        self.events.append(("hot", provider_id))
        if provider_id == "robot_arm.primary.contact" and self.fail_contact_hot:
            raise RuntimeError("simulated Contact HOT failure")
        return {"provider_id": provider_id, "residency": "HOT"}

    async def set_residency(self, provider_id, action):
        self.events.append((action, provider_id))
        if (
            provider_id == "robot_arm.primary.integrated"
            and action == "warm"
        ):
            if self.fail_integrated_warm:
                raise RuntimeError("simulated Integrated WARM failure")
            if self.integrated is not None:
                self.integrated.residency = "WARM"
                self.integrated.lease_active = False
        return {"provider_id": provider_id, "residency": action.upper()}

    async def workcell_calibrations(self):
        self.events.append(("calibrations", None))
        value = self.activations.pop(0) if len(self.activations) > 1 else self.activations[0]
        return {"activations": [value]}


class FakeFabric:
    def __init__(self, profile):
        self.profile = copy.deepcopy(profile)

    async def latest_optional(self, stream):
        if stream != "robot_arm.assembly_state":
            return None
        return {
            "provider_id": "robot_arm.rebot_dm",
            "data": {
                "schema": "midbrain.robot_assembly_state",
                "schema_version": 1,
                "resource_groups": [
                    {
                        "group_id": "arm",
                        "joint_names": [
                            "joint1",
                            "joint2",
                            "joint3",
                            "joint4",
                            "joint5",
                            "joint6",
                        ],
                    }
                ],
                "mounted_effector": copy.deepcopy(self.profile),
            },
        }


class FakeIntegrated:
    def __init__(self, events, result=None, measured_position=None):
        self.events = events
        self.measured_position = measured_position or [0.4, 0.5, 0.6]
        self.measured_rpy = [0.0, 0.0, 0.0]
        self.result = result or {
            "workflow_complete": True,
            "physical_motion_completed": True,
            "goal_reached": True,
            "final_state": "FLOAT",
            "preview_id": "preview-1",
            "controller_preview_id": "preview-1",
        }
        self.plan_id = "preview-1"
        self.residency = "HOT"
        self.lease_active = True

    async def preview(self, **arguments):
        self.events.append(("integrated_preview", arguments))
        self.measured_rpy = list(arguments["target_orientation_rpy_rad"])
        return {"status": "PREVIEW_READY", "preview_id": "preview-1"}

    async def execute_preview(self, *, preview_id):
        self.events.append(("integrated_execute", preview_id))
        return dict(self.result)

    async def observation(self):
        self.events.append(("integrated_observation", None))
        return {
            "controller": {
                "boot_id": "integrated-boot-1",
                "provider_instance_id": "integrated-instance-1",
                "residency": self.residency,
                "lease": {"active": self.lease_active},
                "safety": {"float_confirmed": True},
                "trajectory": {"active": False},
                "planning": {
                    "last_authorized_transit": {"plan_id": self.plan_id}
                },
                "model_view": {
                    "measured_controlled_frame": {
                        "position_m": list(self.measured_position),
                        "rpy_rad": list(self.measured_rpy),
                    }
                },
            }
        }


class FakeContactRuntime:
    def __init__(self, events):
        self.events = events

    def execute(self, skill_id, steps):
        self.events.append(("contact_execute", skill_id, len(steps)))
        return {
            "skill_id": skill_id,
            "submitted_step_count": len(steps),
            "relax": {"disposition": "EXPLICITLY_RELAXED"},
        }


class SlicingHostAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.effector_path = root / "effector.json"
        self.motion_path = root / "motion.json"
        self.motion_template_path = root / "motion.default.json"
        self.effector_path.write_text(json.dumps(effector_profile()), encoding="utf-8")
        self.motion_template_path.write_text(json.dumps(motion_profiles()), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def adapter(
        self,
        events,
        *,
        manager=None,
        integrated=None,
        fabric=None,
    ):
        integrated_client = integrated or FakeIntegrated(events)
        manager_client = manager or FakeManager(events, [activation()])
        if hasattr(manager_client, "integrated"):
            manager_client.integrated = integrated_client
        return SlicingHostAdapter(
            manager=manager_client,
            fabric=fabric or FakeFabric(effector_profile()),
            integrated_motion=integrated_client,
            contact_provider_url="http://contact",
            effector_profile_path=self.effector_path,
            motion_profiles_path=self.motion_path,
            motion_profiles_template_path=self.motion_template_path,
            contact_runtime_factory=lambda: FakeContactRuntime(events),
        )

    async def test_developer_stage_gate_rejects_contact_before_alignment(self):
        events = []
        adapter = self.adapter(events)
        session = await adapter.prepare_development(
            developer_arguments(), point_mode="ABSOLUTE_WORLD"
        )
        with self.assertRaisesRegex(RuntimeError, "requires CONTACT_READY"):
            await adapter.execute_development_contact(session["session_id"])
        self.assertFalse(any(item[0] == "contact_execute" for item in events))

    async def test_relative_begin_point_is_frozen_from_current_origin(self):
        events = []
        data = developer_arguments()
        data["slice_begin_point_m"] = [0.1, 0.0, -0.2]
        adapter = self.adapter(
            events,
            integrated=FakeIntegrated(events, measured_position=[0.4, 0.5, 0.6]),
        )
        session = await adapter.prepare_development(
            data, point_mode="RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
        )
        self.assertEqual(
            session["point_resolution"]["captured_current_effector_world_m"],
            [0.4, 0.5, 0.6],
        )
        self.assertAlmostEqual(
            session["resolved_arguments"]["slice_begin_point_m"][2],
            0.4,
        )

    async def test_developer_runs_alignment_then_contact_as_separate_calls(self):
        events = []
        adapter = self.adapter(events)
        data = developer_arguments()
        data["integrated_execution_backend"] = "POS_SPEED"
        prepared = await adapter.prepare_development(
            data, point_mode="ABSOLUTE_WORLD"
        )
        preview_event = next(event for event in events if event[0] == "integrated_preview")
        self.assertEqual(preview_event[1]["execution_backend"], "POS_SPEED")
        aligned = await adapter.execute_development_alignment(prepared["session_id"])
        self.assertEqual(aligned["state"], "CONTACT_READY")
        self.assertIn("slicing_handoff", aligned["alignment_result"])
        completed = await adapter.execute_development_contact(prepared["session_id"])
        self.assertEqual(completed["state"], "COMPLETE_RELAX_REQUESTED")
        self.assertEqual(events[-1], ("contact_execute", "contact.slicing", 3))
        self.assertLess(
            events.index(("warm", "robot_arm.primary.integrated")),
            events.index(("hot", "robot_arm.primary.contact")),
        )
        self.assertLess(
            events.index(("hot", "robot_arm.primary.contact")),
            events.index(("contact_execute", "contact.slicing", 3)),
        )

    async def test_float_handoff_allows_substantial_orientation_drift(self):
        events = []
        integrated = FakeIntegrated(events)
        adapter = self.adapter(events, integrated=integrated)
        prepared = await adapter.prepare_development(
            developer_arguments(), point_mode="ABSOLUTE_WORLD"
        )
        await adapter.execute_development_alignment(prepared["session_id"])
        integrated.measured_rpy[0] += 0.2
        completed = await adapter.execute_development_contact(prepared["session_id"])
        self.assertEqual(completed["state"], "COMPLETE_RELAX_REQUESTED")

    async def test_float_handoff_rejects_large_drift_before_contact(self):
        events = []
        integrated = FakeIntegrated(events)
        adapter = self.adapter(events, integrated=integrated)
        prepared = await adapter.prepare_development(
            developer_arguments(), point_mode="ABSOLUTE_WORLD"
        )
        await adapter.execute_development_alignment(prepared["session_id"])
        integrated.measured_rpy[0] += 0.5
        with self.assertRaisesRegex(RuntimeError, "maximum_orientation_drift"):
            await adapter.execute_development_contact(prepared["session_id"])
        observation = await adapter.development_observation()
        failed = next(
            item for item in observation["sessions"]
            if item["session_id"] == prepared["session_id"]
        )
        self.assertEqual(failed["state"], "CONTACT_NOT_STARTED_PREFLIGHT_REJECTED")
        self.assertNotIn(("hot", "robot_arm.primary.contact"), events)

    async def test_agent_defaults_use_profile_one(self):
        events = []
        result = await self.adapter(events).invoke(agent_arguments())
        self.assertEqual(result["plan"]["blade_profile_number"], 1)
        self.assertEqual(result["plan"]["motion_profile_number"], 1)
        self.assertEqual(events[-1], ("contact_execute", "contact.slicing", 3))

    async def test_next_agent_invocation_uses_live_profile_edits_and_defaults(self):
        events = []
        adapter = self.adapter(events)
        saved_blade = await adapter.save_development_blade_profile(
            name="Live blade",
            blade_direction_effector=[0.0, 0.0, -1.0],
            slicing_direction_effector=[-1.0, 0.0, 0.0],
            locked_joint_names=["joint6"],
        )
        blade_number = saved_blade["saved"]["profile_number"]
        await adapter.set_development_blade_profile_default(blade_number)
        saved_motion = await adapter.save_development_motion_profile(
            name="Live motion",
            blade_load_kgf=3.0,
            retract_distance_m=0.15,
            delay_after_engage_s=0.6,
            slice_wait_speed_m_s=0.25,
            delay_after_retract_s=0.8,
        )
        motion_number = saved_motion["saved"]["profile_number"]
        await adapter.set_development_motion_profile_default(motion_number)

        strict_agent_arguments = agent_arguments()
        strict_agent_arguments["blade_profile_number"] = None
        strict_agent_arguments["motion_profile_number"] = None
        result = await adapter.invoke(strict_agent_arguments)

        self.assertEqual(result["plan"]["blade_profile_number"], blade_number)
        self.assertEqual(result["plan"]["motion_profile_number"], motion_number)
        self.assertEqual(
            result["plan"]["alignment"]["blade_direction_effector"],
            [0.0, 0.0, -1.0],
        )
        self.assertEqual(
            result["plan"]["alignment"]["locked_joint_names"],
            ["joint6"],
        )
        self.assertEqual(result["plan"]["load"]["blade_load_kgf"], 3.0)
        observation = await adapter.development_observation()
        self.assertTrue(observation["blade_profiles_pending_workspace_restart"])
        self.assertTrue(observation["agent_blade_profile_selection_live"])

    async def test_incomplete_alignment_never_starts_contact(self):
        events = []
        integrated = FakeIntegrated(
            events,
            result={
                "workflow_complete": True,
                "physical_motion_completed": True,
                "goal_reached": True,
                "final_state": "WAIT_FOR_NEXT",
                "preview_id": "preview-1",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "accepted target in FLOAT"):
            await self.adapter(events, integrated=integrated).invoke(agent_arguments())
        self.assertNotIn(("hot", "robot_arm.primary.contact"), events)

    async def test_changed_calibration_aborts_before_contact(self):
        events = []
        manager = FakeManager(
            events,
            [activation(), activation(activation_id="activation-2")],
        )
        with self.assertRaisesRegex(RuntimeError, "calibration changed"):
            await self.adapter(events, manager=manager).invoke(agent_arguments())
        self.assertNotIn(("hot", "robot_arm.primary.contact"), events)

    async def test_integrated_warm_failure_prevents_contact_activation(self):
        events = []
        manager = FakeManager(
            events,
            [activation()],
            fail_integrated_warm=True,
        )
        adapter = self.adapter(events, manager=manager)
        prepared = await adapter.prepare_development(
            developer_arguments(), point_mode="ABSOLUTE_WORLD"
        )
        await adapter.execute_development_alignment(prepared["session_id"])
        with self.assertRaisesRegex(
            RuntimeError,
            "provider transition failed before a Contact session",
        ):
            await adapter.execute_development_contact(prepared["session_id"])
        self.assertNotIn(("hot", "robot_arm.primary.contact"), events)
        self.assertFalse(any(item[0] == "contact_execute" for item in events))
        observation = await adapter.development_observation()
        failed = next(
            item
            for item in observation["sessions"]
            if item["session_id"] == prepared["session_id"]
        )
        self.assertEqual(
            failed["state"],
            "CONTACT_NOT_STARTED_PREFLIGHT_REJECTED",
        )

    async def test_unreleased_integrated_lease_prevents_contact_activation(self):
        events = []
        manager = FakeManager(events, [activation()])
        integrated = FakeIntegrated(events)
        manager.integrated = None
        adapter = SlicingHostAdapter(
            manager=manager,
            fabric=FakeFabric(effector_profile()),
            integrated_motion=integrated,
            contact_provider_url="http://contact",
            effector_profile_path=self.effector_path,
            motion_profiles_path=self.motion_path,
            motion_profiles_template_path=self.motion_template_path,
            contact_runtime_factory=lambda: FakeContactRuntime(events),
        )
        prepared = await adapter.prepare_development(
            developer_arguments(), point_mode="ABSOLUTE_WORLD"
        )
        await adapter.execute_development_alignment(prepared["session_id"])
        with self.assertRaisesRegex(
            RuntimeError,
            "lease handoff was not confirmed",
        ):
            await adapter.execute_development_contact(prepared["session_id"])
        self.assertNotIn(("hot", "robot_arm.primary.contact"), events)
        self.assertFalse(any(item[0] == "contact_execute" for item in events))

    async def test_profile_crud_reuses_gaps_allows_one_and_sets_defaults(self):
        events = []
        adapter = self.adapter(events)
        saved_blade = await adapter.save_development_blade_profile(
            name="Alternate",
            blade_direction_effector=[0.0, 0.0, -1.0],
            slicing_direction_effector=[-1.0, 0.0, 0.0],
            locked_joint_names=["joint6"],
        )
        self.assertEqual(saved_blade["saved"]["profile_number"], 2)
        self.assertEqual(saved_blade["saved"]["locked_joint_names"], ["joint6"])
        self.assertTrue(
            saved_blade["profile_status"]["pending_workspace_restart"]
        )
        await adapter.set_development_blade_profile_default(2)
        deleted_blade = await adapter.delete_development_blade_profile(1)
        self.assertEqual(
            deleted_blade["profile_status"]["source"]["default_profile_number"],
            2,
        )
        replacement_blade = await adapter.save_development_blade_profile(
            name="Replacement one",
            blade_direction_effector=[0.0, 0.0, -1.0],
            slicing_direction_effector=[-1.0, 0.0, 0.0],
            locked_joint_names=[],
        )
        self.assertEqual(replacement_blade["saved"]["profile_number"], 1)

        saved_motion = await adapter.save_development_motion_profile(
            name="Slower",
            blade_load_kgf=1.5,
            retract_distance_m=0.1,
            delay_after_engage_s=0.5,
            slice_wait_speed_m_s=0.2,
            delay_after_retract_s=0.6,
        )
        self.assertEqual(saved_motion["saved"]["profile_number"], 2)
        await adapter.set_development_motion_profile_default(2)
        deleted_motion = await adapter.delete_development_motion_profile(1)
        self.assertEqual(
            deleted_motion["motion_profiles"]["default_profile_number"],
            2,
        )
        replacement_motion = await adapter.save_development_motion_profile(
            name="Replacement one",
            blade_load_kgf=1.5,
            retract_distance_m=0.1,
            delay_after_engage_s=0.5,
            slice_wait_speed_m_s=0.2,
            delay_after_retract_s=0.6,
        )
        self.assertEqual(replacement_motion["saved"]["profile_number"], 1)

    async def test_blade_profile_rejects_lock_outside_active_arm_group(self):
        adapter = self.adapter([])
        with self.assertRaisesRegex(ValueError, "outside the active arm group"):
            await adapter.save_development_blade_profile(
                name="Invalid lock",
                blade_direction_effector=[0.0, 0.0, -1.0],
                slicing_direction_effector=[-1.0, 0.0, 0.0],
                locked_joint_names=["joint7"],
            )


if __name__ == "__main__":
    unittest.main()
