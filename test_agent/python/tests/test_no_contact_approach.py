from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import unittest
from types import SimpleNamespace

import numpy as np

from physical_agent_test.authorization import AuthorizationStore
from physical_agent_test.no_contact_approach import (
    NoContactItemApproachAdapter,
    build_no_contact_correction_plan,
    build_no_contact_preview_context,
)


def item(point=(0.5, 0.0, 0.25), *, observed_at_us=1_000_000):
    return {
        "eligible_for_control_math": True,
        "status": "METRIC_POINT_READY",
        "target_frame": "rebot_arm_base",
        "calibration_revision": "cal-1",
        "observed_at_us": observed_at_us,
        "object_id": "toilet-paper",
        "contact_policy": "WORKPIECE_CONTACT_ALLOWED",
        "location": {
            "target_point_m": list(point),
            "uncertainty_radius_m": 0.01,
        },
    }


def effector(point=(0.2, 0.0, 0.25), *, observed_at_us=1_000_500):
    return {
        "eligible_for_control_math": True,
        "status": "REFERENCE_READY",
        "target_frame": "rebot_arm_base",
        "calibration_revision": "cal-1",
        "observed_at_us": observed_at_us,
        "control_reference": {"target_point_m": list(point)},
        "controller_consistency": {
            "decision": "ACCEPT",
            "controller_reference": {"target_point_m": list(point)},
        },
        "front_points": [
            {"depth_evidence": {"support_mad_m": 0.002}}
        ],
    }


def preview_item(
    point=(0.5, 0.0, 0.25),
    *,
    observed_at_us: int,
):
    result = item(point, observed_at_us=observed_at_us)
    result.update(
        {
            "capability_binding": {
                "provider_id": "camera.test",
                "provider_instance_id": "camera-instance-1",
                "boot_id": "camera-boot-1",
                "binding": {"binding_id": "binding-1"},
            },
            "camera_capture": {"session_epoch": "vio-epoch-1"},
            "calibration_revision": "camera-calibration-1",
        }
    )
    return result


def preview_effector(*, observed_at_us: int):
    result = effector(observed_at_us=observed_at_us)
    result.update(
        {
            "calibration_revision": "camera-calibration-1",
            "capability_binding": {
                "provider_id": "camera.test",
                "provider_instance_id": "camera-instance-1",
                "boot_id": "camera-boot-1",
                "binding": {"binding_id": "effector-binding-1"},
            },
            "camera_capture": {"session_epoch": "vio-epoch-1"},
        }
    )
    return result


def current_scene(*, now_us: int):
    return {
        "status": "SCENE_READY",
        "scene_revision": "scene-1",
        "expires_at_us": now_us + 5_000_000,
    }


def current_calibrations():
    return {
        "activations": [
            {
                "state": "ACTIVE",
                "motion_usable": True,
                "expires_at_us": None,
                "validity_policy": "MOUNTED_IDENTITY_TRACKING_GATED_V1",
                "session_epoch": "vio-epoch-1",
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "camera-instance-1",
                "camera_boot_id": "camera-boot-1",
                "camera_calibration_revision": "camera-calibration-1",
                "candidate_id": "workcell-1",
                "calibration_revision": "workcell-revision-1",
            }
        ]
    }


class NoContactPlanTests(unittest.TestCase):
    def test_default_correction_can_exceed_former_twenty_centimeter_cap(
        self,
    ):
        plan = build_no_contact_correction_plan(
            item_location=item((0.6, 0.0, 0.25)),
            effector_location=effector(),
            requested_standoff_m=0.1,
            iteration_index=0,
        )

        self.assertAlmostEqual(plan["step_distance_m"], 0.3)
        self.assertAlmostEqual(
            plan["controller_plan_request"]["requested_speed_m_s"],
            0.30,
        )

    def test_visual_effector_without_fk_still_produces_bounded_step(self):
        visual_effector = effector((0.2, 0.0, 0.25))
        visual_effector["controller_consistency"] = {
            "decision": "ACCEPT_DEGRADED_NO_CONTROLLER_FK",
            "controller_reference": None,
        }
        visual_effector["uncertainty_radius_m"] = 0.04

        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=visual_effector,
            requested_standoff_m=0.1,
            iteration_index=0,
            maximum_step_m=0.05,
            planned_at_us=2_000_000,
        )

        self.assertEqual(plan["status"], "CORRECTION_STEP_READY")
        self.assertAlmostEqual(plan["step_distance_m"], 0.05)
        self.assertEqual(
            plan["controller_reference_source"],
            "VISUAL_EFFECTOR_ABSOLUTE_FALLBACK_NO_CONTROLLER_FK",
        )
        target = plan["controller_plan_request"]["target"]
        self.assertNotIn("position_m", target)
        self.assertAlmostEqual(
            float(np.linalg.norm(target["position_delta_m"])),
            0.05,
        )
        self.assertEqual(
            plan["controller_target_policy"],
            "INTEGRATED_RESOLVES_DELTA_FROM_MEASURED_CONTROLLED_FRAME",
        )

    def test_caps_each_step_and_requires_reobservation(self):
        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=effector(),
            requested_standoff_m=0.1,
            iteration_index=0,
            maximum_step_m=0.05,
            planned_at_us=2_000_000,
        )

        self.assertEqual(plan["status"], "CORRECTION_STEP_READY")
        self.assertAlmostEqual(plan["step_distance_m"], 0.05)
        self.assertEqual(plan["next_action"], "MOVE_THEN_REOBSERVE_BOTH")
        self.assertEqual(
            plan["contact_policy"]["allowed_contact_object_ids"],
            [],
        )
        self.assertFalse(plan["physical_motion_authorized"])
        self.assertGreaterEqual(
            plan["predicted_distance_to_item_m"],
            plan["effective_standoff_m"],
        )

    def test_applies_visual_correction_delta_to_controller_tool_origin(self):
        visual_effector = effector((0.2, 0.0, 0.25))
        visual_effector["controller_consistency"]["controller_reference"] = {
            "target_point_m": [0.3, 0.0, 0.25]
        }

        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=visual_effector,
            requested_standoff_m=0.1,
            iteration_index=0,
            maximum_step_m=0.05,
            planned_at_us=2_000_000,
        )

        self.assertEqual(
            plan["controller_target_policy"],
            "APPLY_EFFECTOR_CORRECTION_DELTA_TO_CONTROLLER_FK",
        )
        self.assertEqual(
            plan["predicted_effector_point_arm_base_m"],
            [0.25, 0.0, 0.25],
        )
        self.assertEqual(plan["next_target_arm_base_m"], [0.35, 0.0, 0.25])
        self.assertEqual(
            plan["controller_plan_request"]["target"]["position_m"],
            [0.35, 0.0, 0.25],
        )

    def test_aligned_plan_submits_no_motion(self):
        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=effector((0.4, 0.0, 0.25)),
            requested_standoff_m=0.1,
            iteration_index=2,
        )

        self.assertEqual(plan["status"], "ALIGNED_AT_NO_CONTACT_STANDOFF")
        self.assertTrue(plan["workflow_complete"])
        self.assertIsNone(plan["controller_plan_request"])

    def test_uncertainty_is_reported_without_adding_clearance(self):
        uncertain = item()
        uncertain["location"]["uncertainty_radius_m"] = 0.09
        plan = build_no_contact_correction_plan(
            item_location=uncertain,
            effector_location=effector(),
            requested_standoff_m=0.05,
            iteration_index=0,
        )

        self.assertAlmostEqual(plan["effective_standoff_m"], 0.05)
        self.assertAlmostEqual(plan["combined_uncertainty_m"], 0.095)

    def test_zero_work_object_standoff_is_valid(self):
        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=effector(),
            requested_standoff_m=0.0,
            iteration_index=0,
        )

        self.assertEqual(plan["effective_standoff_m"], 0.0)
        self.assertEqual(plan["requested_standoff_m"], 0.0)

    def test_no_descent_policy_never_descends_toward_lower_item(self):
        plan = build_no_contact_correction_plan(
            item_location=item((0.5, 0.0, 0.10)),
            effector_location=effector((0.2, 0.0, 0.30)),
            requested_standoff_m=0.1,
            iteration_index=0,
            vertical_policy="NO_DESCENT",
        )

        self.assertEqual(plan["vertical_policy"], "NO_DESCENT")
        self.assertAlmostEqual(plan["next_target_arm_base_m"][2], 0.30)

    def test_default_free_3d_policy_retains_vertical_correction(self):
        plan = build_no_contact_correction_plan(
            item_location=item((0.5, 0.0, 0.10)),
            effector_location=effector((0.2, 0.0, 0.30)),
            requested_standoff_m=0.1,
            iteration_index=0,
        )

        self.assertLess(plan["next_target_arm_base_m"][2], 0.30)
        self.assertEqual(plan["vertical_policy"], "FREE_3D")
        self.assertEqual(
            plan["controller_plan_request"]["ik_mode"],
            "POSE_6DOF",
        )

    def test_rejects_skewed_observations(self):
        with self.assertRaisesRegex(ValueError, "OBSERVATION_SKEW"):
            build_no_contact_correction_plan(
                item_location=item(observed_at_us=1_000_000),
                effector_location=effector(observed_at_us=4_000_000),
                requested_standoff_m=0.1,
                iteration_index=0,
            )

    def test_arm_radius_is_advisory_and_ik_owns_reachability(self):
        plan = build_no_contact_correction_plan(
            item_location=item(),
            effector_location=effector((-2.2, -0.5, -0.6)),
            requested_standoff_m=0.1,
            iteration_index=0,
        )

        self.assertEqual(
            plan["arm_radius_policy"],
            "ADVISORY_ONLY_IK_JOINT_LIMITS_AND_SEMANTIC_SCENE_ARE_AUTHORITATIVE",
        )
        self.assertIsNotNone(plan["controller_plan_request"])

    def test_preview_context_binds_scene_camera_and_mount_identities(self):
        now_us = time.time_ns() // 1000
        context = build_no_contact_preview_context(
            item_location=preview_item(observed_at_us=now_us - 20_000),
            effector_location=preview_effector(observed_at_us=now_us - 10_000),
            scene=current_scene(now_us=now_us),
            workcell_calibrations=current_calibrations(),
        )

        self.assertEqual(context["binding_id"], "binding-1")
        self.assertEqual(context["scene_revision"], "scene-1")
        self.assertEqual(context["workcell_transform_id"], "workcell-1")
        self.assertEqual(
            context["workcell_transform_validity_policy"],
            "MOUNTED_IDENTITY_TRACKING_GATED_V1",
        )

    def test_preview_context_rejects_camera_identity_mismatch(self):
        now_us = time.time_ns() // 1000
        calibrations = current_calibrations()
        calibrations["activations"][0]["camera_boot_id"] = "old-boot"

        with self.assertRaisesRegex(
            ValueError,
            "NO_CURRENT_EXACT_WORKCELL_ACTIVATION",
        ):
            build_no_contact_preview_context(
                item_location=preview_item(observed_at_us=now_us - 20_000),
                effector_location=preview_effector(
                    observed_at_us=now_us - 10_000
                ),
                scene=current_scene(now_us=now_us),
                workcell_calibrations=calibrations,
            )

    def test_preview_context_rejects_cross_observation_camera_mismatch(self):
        now_us = time.time_ns() // 1000
        mismatched_effector = preview_effector(
            observed_at_us=now_us - 10_000
        )
        mismatched_effector["capability_binding"]["boot_id"] = "new-boot"

        with self.assertRaisesRegex(
            ValueError,
            "NO_CONTACT_OBSERVATION_CAMERA_IDENTITY_MISMATCH",
        ):
            build_no_contact_preview_context(
                item_location=preview_item(observed_at_us=now_us - 20_000),
                effector_location=mismatched_effector,
                scene=current_scene(now_us=now_us),
                workcell_calibrations=current_calibrations(),
            )

    def test_preview_context_accepts_v2_after_camera_and_vio_restart(self):
        now_us = time.time_ns() // 1000
        calibrations = current_calibrations()
        activation = calibrations["activations"][0]
        activation["validity_policy"] = (
            "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
        )
        activation["camera_provider_instance_id"] = "prior-instance"
        activation["camera_boot_id"] = "prior-boot"
        activation["session_epoch"] = "historical-vio-epoch"

        context = build_no_contact_preview_context(
            item_location=preview_item(observed_at_us=now_us - 20_000),
            effector_location=preview_effector(observed_at_us=now_us - 10_000),
            scene=current_scene(now_us=now_us),
            workcell_calibrations=calibrations,
        )

        self.assertEqual(
            context["workcell_transform_validity_policy"],
            "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2",
        )
        self.assertEqual(
            context["camera_calibration_revision"],
            "camera-calibration-1",
        )

    def test_preview_context_accepts_v2_without_vio_epoch(self):
        now_us = time.time_ns() // 1000
        calibrations = current_calibrations()
        activation = calibrations["activations"][0]
        activation["validity_policy"] = (
            "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
        )
        item_location = preview_item(observed_at_us=now_us - 20_000)
        effector_location = preview_effector(
            observed_at_us=now_us - 10_000
        )
        item_location["camera_capture"].pop("session_epoch")
        effector_location["camera_capture"].pop("session_epoch")

        context = build_no_contact_preview_context(
            item_location=item_location,
            effector_location=effector_location,
            scene=current_scene(now_us=now_us),
            workcell_calibrations=calibrations,
        )

        self.assertIsNone(context["vio_session_epoch"])
        self.assertEqual(
            context["vio_session_epoch_policy"],
            "ADVISORY_FOR_MOUNTED_V2",
        )
        self.assertEqual(
            context["context_advisories"],
            [
                "VIO_SESSION_EPOCH_UNAVAILABLE_NOT_REQUIRED_FOR_MOUNTED_V2"
            ],
        )

    def test_preview_context_still_requires_vio_epoch_for_v1(self):
        now_us = time.time_ns() // 1000
        item_location = preview_item(observed_at_us=now_us - 20_000)
        effector_location = preview_effector(
            observed_at_us=now_us - 10_000
        )
        item_location["camera_capture"].pop("session_epoch")
        effector_location["camera_capture"].pop("session_epoch")

        with self.assertRaisesRegex(
            ValueError,
            "NO_CURRENT_EXACT_WORKCELL_ACTIVATION|vio_session_epoch",
        ):
            build_no_contact_preview_context(
                item_location=item_location,
                effector_location=effector_location,
                scene=current_scene(now_us=now_us),
                workcell_calibrations=current_calibrations(),
            )


class FakeLocator:
    def __init__(self, result):
        self.result = result
        self.started = asyncio.Event()
        self.peer = None

    async def run(self, **_arguments):
        self.started.set()
        if self.peer is not None:
            await asyncio.wait_for(self.peer.started.wait(), timeout=0.2)
        return self.result


class SharedContextSpatial:
    def __init__(self):
        self.calls = 0
        self.context = SimpleNamespace(target_frame="rebot_arm_base")

    async def prepare_context(self, **_arguments):
        self.calls += 1
        return self.context


class SharedContextLocator(FakeLocator):
    def __init__(self, result, spatial):
        super().__init__(result)
        self.spatial = spatial
        self.received_context = None

    async def run(self, **arguments):
        self.received_context = arguments.get("spatial_context")
        return await super().run(**arguments)


class SequenceLocator:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def run(self, **_arguments):
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


class _Response:
    status_code = 404


class MissingArmTransformLocator:
    async def run(self, **_arguments):
        error = RuntimeError(
            "404 for http://127.0.0.1:7002/v1/transform?"
            "from_frame=camera&to_frame=rebot_arm_base"
        )
        error.response = _Response()
        raise error


class FakeSceneInspector:
    def __init__(self, result):
        self.result = result
        self.include_spheres = None

    async def run(self, *, include_spheres=False):
        self.include_spheres = include_spheres
        return self.result


class FakeManager:
    async def workcell_calibrations(self):
        return current_calibrations()


class FakeIntegrated:
    def __init__(self):
        self.request = None

    async def preview_transit_path(self, request):
        self.request = request
        return {
            "status": "REJECTED",
            "physical_motion_authorized": False,
        }


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AcceptedFakeIntegrated(FakeIntegrated):
    def __init__(self, *, closest_safe: bool = False):
        super().__init__()
        self.commit = None
        self.release_count = 0
        self.closest_safe = bool(closest_safe)

    async def preview_transit_path(self, request):
        self.request = request
        now_us = time.time_ns() // 1000
        normalized_request = {
            "target": copy.deepcopy(request["target"]),
            "requested_speed_m_s": float(request["requested_speed_m_s"]),
            "allowed_contact_object_ids": [],
            "permit_pushable_contact": False,
            "final_state": str(request["final_state"]),
            "request_context": copy.deepcopy(request["request_context"]),
            "ik_mode": "POSE_6DOF",
        }
        preview = {
            "status": "PLANNED",
            "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
            "enforcement": "SHADOW_NONPHYSICAL",
            "physical_motion_authorized": False,
            "control_state_unchanged": True,
            "lease_unchanged": True,
            "goal_reached": not self.closest_safe,
            "closest_safe": self.closest_safe,
            "motion_outcome": (
                "CLOSEST_SAFE" if self.closest_safe else "GOAL_REACHED"
            ),
            "plan_id": "no-contact-preview-1",
            "selected_plan": {
                "planning_valid": True,
                "goal_reached": not self.closest_safe,
                "closest_safe": self.closest_safe,
                "closest_safe_reason": (
                    "REQUESTED_GOAL_BLOCKED_BY_SEMANTIC_GEOMETRY"
                    if self.closest_safe
                    else None
                ),
                "blocking_object_ids": (
                    ["table"] if self.closest_safe else []
                ),
                "blocking_object_types": (
                    ["KEEP_OUT"] if self.closest_safe else []
                ),
                "remaining_position_to_goal_m": (
                    0.012 if self.closest_safe else 0.0
                ),
                "executed_controlled_displacement_m": (
                    0.288 if self.closest_safe else 0.3
                ),
                "preview": {
                    "collision_free": True,
                    "preview_id": "no-contact-preview-1",
                },
            },
            "scene_revision": "scene-1",
            "final_state": str(request["final_state"]),
        }
        contract = {
            "schema": "physical_agent.integrated_transit_preview_contract",
            "schema_version": 1,
            "preview_id": "no-contact-preview-1",
            "issued_at_us": now_us,
            "expires_at_us": now_us + 30_000_000,
            "controller_provider_id": "robot_arm.primary.integrated",
            "controller_provider_instance_id": "integrated-instance-1",
            "controller_boot_id": "integrated-boot-1",
            "controller_configuration_sha256": "configuration-sha-1",
            "request_sha256": _canonical_sha256(normalized_request),
            "normalized_request": normalized_request,
            "request_context_sha256": _canonical_sha256(
                normalized_request["request_context"]
            ),
            "request_context_complete": True,
            "request_context_issues": [],
            "scene_revision": "scene-1",
            "lease_snapshot": {"active": False},
            "physical_motion_authorized": False,
            "preview_grants_commit_authority": False,
            "commit_endpoint_exposed": False,
        }
        contract["preview_sha256"] = _canonical_sha256(
            {
                "planning_result": preview,
                "preview_contract": contract,
            }
        )
        preview["preview_contract"] = contract
        return preview

    async def commit_transit_path(self, payload, *, authorization_assertion):
        self.commit = {
            "payload": payload,
            "authorization_assertion": authorization_assertion,
        }
        return {
            "status": "EXECUTING",
            "planned_duration_s": 0.1,
            "final_state": "WAIT_FOR_NEXT",
        }

    async def state(self):
        return {
            "planning": {
                "authorized_transit": {
                    "plan_id": "no-contact-preview-1",
                    "status": "WAITING_NEXT",
                    "final_state": "WAIT_FOR_NEXT",
                    "completed_stage_count": 1,
                }
            },
            "safety": {"float_confirmed": self.release_count > 0},
        }

    async def release_transit_path(self):
        self.release_count += 1
        return {"status": "gravity_float"}


class StaleOnceFakeIntegrated(AcceptedFakeIntegrated):
    def __init__(self):
        super().__init__()
        self.commit_calls = 0

    async def commit_transit_path(self, payload, *, authorization_assertion):
        self.commit_calls += 1
        if self.commit_calls == 1:
            raise RuntimeError(
                "403 semantic scene is stale at authorized transit commit"
            )
        return await super().commit_transit_path(
            payload,
            authorization_assertion=authorization_assertion,
        )


class NoContactAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_observation_is_automatically_reacquired(self):
        rejected = effector()
        rejected.update(
            {
                "status": "CONTROLLER_CONSISTENCY_REJECTED",
                "eligible_for_control_math": False,
            }
        )
        item_locator = SequenceLocator([item(), item()])
        effector_locator = SequenceLocator([rejected, effector()])
        adapter = NoContactItemApproachAdapter(
            item_locator,
            effector_locator,
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["status"], "CORRECTION_STEP_READY")
        self.assertEqual(item_locator.calls, 2)
        self.assertEqual(effector_locator.calls, 2)

    async def test_missing_scene_requests_hot_provider_and_exact_retry(self):
        now_us = time.time_ns() // 1000
        adapter = NoContactItemApproachAdapter(
            FakeLocator(preview_item(observed_at_us=now_us - 20_000)),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=FakeSceneInspector({"status": "NO_SCENE"}),
            manager=FakeManager(),
            integrated=FakeIntegrated(),
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
            vertical_policy="PRESERVE_CURRENT_HEIGHT",
        )

        self.assertEqual(result["status"], "SEMANTIC_SCENE_PROVIDER_REQUIRED")
        self.assertEqual(
            result["required_next_tool"]["arguments"]["provider_id"],
            "world_model.arm_scene_compiler",
        )
        self.assertEqual(
            result["retry_after_prerequisite"]["arguments"]["vertical_policy"],
            "PRESERVE_CURRENT_HEIGHT",
        )

    async def test_missing_tracker_coverage_requests_tracker_before_compiler(
        self,
    ):
        now_us = time.time_ns() // 1000
        adapter = NoContactItemApproachAdapter(
            FakeLocator(preview_item(observed_at_us=now_us - 20_000)),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=FakeSceneInspector(
                {
                    "status": "TRACKER_COVERAGE_REQUIRED",
                    "required_provider_id": "perception.sam2_scene_tracker",
                }
            ),
            manager=FakeManager(),
            integrated=FakeIntegrated(),
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["status"], "SEMANTIC_SCENE_PROVIDER_REQUIRED")
        self.assertEqual(
            result["required_next_tool"]["arguments"]["provider_id"],
            "perception.sam2_scene_tracker",
        )

    async def test_rejected_effector_returns_structured_reobservation(self):
        rejected = effector()
        rejected.update(
            {
                "status": "CONTROLLER_CONSISTENCY_REJECTED",
                "eligible_for_control_math": False,
                "controller_consistency": {"decision": "REJECT"},
            }
        )
        adapter = NoContactItemApproachAdapter(
            FakeLocator(item()),
            FakeLocator(rejected),
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["status"], "EFFECTOR_OBSERVATION_REJECTED")
        self.assertEqual(
            result["next_action"],
            "REOBSERVE_EFFECTOR_WITH_CONTROLLER_PRIOR",
        )
        self.assertFalse(result["motion_submitted"])

    async def test_missing_arm_transform_returns_calibration_prerequisite(self):
        adapter = NoContactItemApproachAdapter(
            MissingArmTransformLocator(),
            MissingArmTransformLocator(),
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(
            result["status"],
            "ARM_BASE_REGISTRATION_REQUIRED",
        )
        self.assertEqual(
            result["required_next_tool"]["name"],
            "calibrate_stationary_workcell",
        )
        self.assertEqual(
            result["retry_after_prerequisite"]["name"],
            "plan_no_contact_item_approach",
        )
        self.assertFalse(result["motion_submitted"])

    async def test_adapter_acquires_item_and_effector_in_parallel(self):
        item_locator = FakeLocator(item())
        effector_locator = FakeLocator(effector())
        item_locator.peer = effector_locator
        effector_locator.peer = item_locator
        adapter = NoContactItemApproachAdapter(
            item_locator,
            effector_locator,
        )

        result = await adapter.run(
            question="locate the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["object_id"], "toilet-paper")
        self.assertTrue(item_locator.started.is_set())
        self.assertTrue(effector_locator.started.is_set())

    async def test_adapter_uses_one_shared_rgbd_context_for_both_landmarks(self):
        spatial = SharedContextSpatial()
        item_locator = SharedContextLocator(item(), spatial)
        effector_locator = SharedContextLocator(effector(), spatial)
        adapter = NoContactItemApproachAdapter(
            item_locator,
            effector_locator,
        )

        result = await adapter.run(
            question="locate the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["object_id"], "toilet-paper")
        self.assertEqual(spatial.calls, 1)
        self.assertIs(item_locator.received_context, spatial.context)
        self.assertIs(effector_locator.received_context, spatial.context)

    async def test_adapter_requests_nonphysical_controller_preview(self):
        now_us = time.time_ns() // 1000
        scene = FakeSceneInspector(current_scene(now_us=now_us))
        integrated = FakeIntegrated()
        adapter = NoContactItemApproachAdapter(
            FakeLocator(preview_item(observed_at_us=now_us - 20_000)),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=scene,
            manager=FakeManager(),
            integrated=integrated,
        )

        result = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )

        self.assertEqual(result["status"], "CONTROLLER_PREVIEW_REJECTED")
        self.assertEqual(result["correction_status"], "CORRECTION_STEP_READY")
        self.assertIsNotNone(integrated.request)
        self.assertFalse(integrated.request["execute"])
        self.assertFalse(integrated.request["physical_motion_authorized"])
        self.assertEqual(
            integrated.request["request_context"]["scene_revision"],
            "scene-1",
        )
        self.assertFalse(scene.include_spheres)

    async def test_exact_preview_can_execute_then_requires_reobservation(self):
        now_us = time.time_ns() // 1000
        integrated = AcceptedFakeIntegrated()
        adapter = NoContactItemApproachAdapter(
            FakeLocator(
                preview_item(
                    (0.6, 0.0, 0.25),
                    observed_at_us=now_us - 20_000,
                )
            ),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=FakeSceneInspector(current_scene(now_us=now_us)),
            manager=FakeManager(),
            integrated=integrated,
            authorization_store=AuthorizationStore("a" * 32),
        )

        plan = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )
        self.assertEqual(
            plan["required_next_tool"]["arguments"],
            {},
        )
        canonical = await adapter.pending_execution_authorization_arguments(
            "no-contact-preview-1"
        )
        self.assertIsNotNone(canonical)
        self.assertAlmostEqual(canonical["distance_m"], 0.4)
        self.assertGreater(canonical["distance_m"], 0.2)
        self.assertEqual(canonical["motion_intent"], "NEW_RELATIVE_MOVE")
        self.assertEqual(canonical["direction"], "TARGET_VECTOR")
        self.assertEqual(
            canonical["orientation_policy"],
            "PRESERVE_CURRENT",
        )
        execution = await adapter.execute_current_preview()

        self.assertEqual(plan["status"], "CONTROLLER_PREVIEW_READY")
        self.assertEqual(execution["status"], "COMPLETED")
        self.assertTrue(execution["motion_submitted"])
        self.assertTrue(execution["measured_arrival_confirmed"])
        self.assertTrue(execution["post_move_reobservation_required"])
        self.assertFalse(
            execution["integrated_controller"][
                "gravity_float_confirmed"
            ]
        )
        self.assertEqual(
            execution["integrated_controller"]["terminal_status"],
            "WAITING_NEXT",
        )
        self.assertTrue(
            execution["integrated_controller"][
                "measured_arrival_confirmed"
            ]
        )

    async def test_visual_only_effector_relative_preview_can_execute(self):
        now_us = time.time_ns() // 1000
        integrated = AcceptedFakeIntegrated()
        visual_effector = preview_effector(
            observed_at_us=now_us - 10_000
        )
        visual_effector["controller_consistency"] = {
            "decision": "ACCEPT_DEGRADED_NO_CONTROLLER_FK",
            "controller_reference": None,
        }
        visual_effector["uncertainty_radius_m"] = 0.04
        adapter = NoContactItemApproachAdapter(
            FakeLocator(preview_item(observed_at_us=now_us - 20_000)),
            FakeLocator(visual_effector),
            scene_inspector=FakeSceneInspector(current_scene(now_us=now_us)),
            manager=FakeManager(),
            integrated=integrated,
            authorization_store=AuthorizationStore("a" * 32),
        )

        plan = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )
        execution = await adapter.execute_current_preview()

        self.assertEqual(plan["status"], "CONTROLLER_PREVIEW_READY")
        self.assertIn(
            "position_delta_m",
            integrated.request["target"],
        )
        self.assertEqual(execution["status"], "COMPLETED")
        self.assertTrue(execution["motion_submitted"])
        self.assertEqual(
            execution["integrated_controller"]["arrival_confirmation"],
            "CONTROLLER_MEASURED_FINAL_POSITION_AND_SETTLED_VELOCITY",
        )
        self.assertEqual(integrated.release_count, 0)
        self.assertEqual(execution["next_action"], "REOBSERVE_BOTH_AND_REPLAN")
        self.assertEqual(
            execution["required_next_tool"]["arguments"]["iteration_index"],
            1,
        )
        self.assertIsNotNone(integrated.commit)
        replay = await adapter.execute_current_preview()
        self.assertEqual(
            replay["status"],
            "NO_CONTACT_CURRENT_PREVIEW_UNAVAILABLE",
        )
        self.assertFalse(replay["motion_submitted"])
        self.assertEqual(
            replay["required_next_tool"]["name"],
            "plan_no_contact_item_approach",
        )

    async def test_closest_safe_execution_is_a_graceful_terminal_result(self):
        now_us = time.time_ns() // 1000
        integrated = AcceptedFakeIntegrated(closest_safe=True)
        adapter = NoContactItemApproachAdapter(
            FakeLocator(
                preview_item(
                    (0.6, 0.0, 0.25),
                    observed_at_us=now_us - 20_000,
                )
            ),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=FakeSceneInspector(current_scene(now_us=now_us)),
            manager=FakeManager(),
            integrated=integrated,
            authorization_store=AuthorizationStore("a" * 32),
        )

        plan = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )
        execution = await adapter.execute_current_preview()

        self.assertTrue(plan["closest_safe"])
        self.assertEqual(execution["status"], "COMPLETED_CLOSEST_SAFE")
        self.assertTrue(execution["workflow_complete"])
        self.assertTrue(execution["closest_safe"])
        self.assertFalse(execution["post_move_reobservation_required"])
        self.assertIsNone(execution["required_next_tool"])
        self.assertEqual(
            execution["next_action"],
            "STOP_AT_NO_CONTACT_BOUNDARY_REQUEST_COMPLETE",
        )
        self.assertEqual(
            execution["closest_safe_report"]["blocking_object_types"],
            ["KEEP_OUT"],
        )
        self.assertAlmostEqual(execution["executed_step_m"], 0.288)

    async def test_stale_scene_commit_is_reobserved_replanned_and_retried(self):
        now_us = time.time_ns() // 1000
        integrated = StaleOnceFakeIntegrated()
        adapter = NoContactItemApproachAdapter(
            FakeLocator(preview_item(observed_at_us=now_us - 20_000)),
            FakeLocator(preview_effector(observed_at_us=now_us - 10_000)),
            scene_inspector=FakeSceneInspector(current_scene(now_us=now_us)),
            manager=FakeManager(),
            integrated=integrated,
            authorization_store=AuthorizationStore("a" * 32),
        )

        plan = await adapter.run(
            question="approach the toilet paper",
            object_id="toilet-paper",
        )
        execution = await adapter.execute_current_preview()

        self.assertEqual(execution["status"], "COMPLETED")
        self.assertEqual(integrated.commit_calls, 2)
        self.assertTrue(
            execution["automatic_commit_recovery"][
                "fresh_item_effector_observation"
            ]
        )


if __name__ == "__main__":
    unittest.main()
