from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rebot_arm_integrated.control_audit import ControlAuditOutbox
from rebot_arm_integrated.service import IntegratedService


class _Controller:
    def __init__(self, scene_revision="scene-1"):
        self.physical_control_call_count = 0
        self.authorization_configured = False
        self.scene_revision = scene_revision

    def set_authorization_assertion_configured(self, configured):
        self.authorization_configured = bool(configured)

    def stage_external_command(self, command, *, source, metadata):
        return {
            "accepted": True,
            "staged_target": {
                "position_m": list(command["target"]["position_m"]),
            },
            "runtime": {"duration_s": 2.0},
        }

    def preview_staged_target(
        self,
        *,
        allowed_contact_object_ids,
        permit_pushable_contact,
    ):
        return {
            "preview_id": "preview-1",
            "planning_valid": True,
            "allowed_contact_object_ids": sorted(allowed_contact_object_ids),
            "permit_pushable_contact": permit_pushable_contact,
        }

    def preview_transit_path(
        self,
        *,
        target_position_m,
        target_delta_m,
        target_rpy_rad,
        requested_speed_m_s,
        execution_backend="IMPEDANCE",
        ik_mode=None,
        allowed_contact_object_ids,
        permit_pushable_contact,
    ):
        return {
            "status": "PLANNED",
            "plan_id": "transit-plan-1",
            "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
            "enforcement": "SHADOW_NONPHYSICAL",
            "physical_motion_authorized": False,
            "control_state_unchanged": True,
            "lease_unchanged": True,
            "target_position_m": (
                None
                if target_position_m is None
                else list(target_position_m)
            ),
            "target_delta_m": (
                None if target_delta_m is None else list(target_delta_m)
            ),
            "target_rpy_rad": (
                None if target_rpy_rad is None else list(target_rpy_rad)
            ),
            "requested_speed_m_s": requested_speed_m_s,
            "execution_backend": execution_backend,
            "ik_mode": ik_mode,
            "allowed_contact_object_ids": sorted(allowed_contact_object_ids),
            "permit_pushable_contact": permit_pushable_contact,
            "selected_plan": {
                "planning_valid": True,
                "planning_reasons": [],
                "q_waypoints_rad": [
                    [0.0] * 6,
                    [0.01, 0.0, 0.0, 0.0, 0.0, 0.0],
                ],
                "preview": {
                    "collision_free": True,
                    "preview_id": "transit-plan-1",
                },
            },
            "scene_revision": self.scene_revision,
        }

    def execute_authorized_transit(self, **kwargs):
        self.physical_control_call_count += 1
        return {
            "status": "EXECUTING",
            "plan_id": kwargs["plan_id"],
            "assertion_id": kwargs["authorization_claims"][
                "assertion_id"
            ],
            "decision_id": kwargs["authorization_claims"][
                "decision_id"
            ],
            "resolved_by": kwargs["authorization_claims"][
                "resolved_by"
            ],
            "physical_motion_authorized": True,
        }

    def snapshot(self):
        return {
            "lease": {
                "active": False,
                "state": "AVAILABLE",
                "lease_id": None,
                "fencing_generation": None,
            },
            "basic_state": {
                "observed_at_us": 123,
                "last_applied_command_id": None,
            },
        }


class ControlAuditTests(unittest.TestCase):
    def test_append_only_audit_preserves_exact_canonical_request_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "path": str(root / "events.jsonl"),
                "cursor_path": str(root / "cursor.json"),
                "strict_local_write": False,
            }
            audit = ControlAuditOutbox(
                root,
                "provider",
                "instance",
                "boot",
                config,
            )
            request = {
                "target": {"position_m": [0.1, 0.2, 0.3]},
                "related_skill_id": "skill-1",
            }

            submitted = audit.record(
                lifecycle="SUBMITTED",
                endpoint="/v1/motion/path-plan",
                command_id="command-1",
                canonical_request=request,
                related_skill_id="skill-1",
            )
            accepted = audit.record(
                lifecycle="ACCEPTED",
                endpoint="/v1/motion/path-plan",
                command_id="command-1",
                canonical_request=request,
                result={"plan_id": "plan-1"},
                related_skill_id="skill-1",
                plan_id="plan-1",
            )

            lines = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            canonical = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(lines[0]["canonical_request"], request)
            self.assertTrue(submitted["local_delivery"]["persisted"])
            self.assertTrue(accepted["local_delivery"]["persisted"])
            self.assertEqual(
                lines[0]["canonical_request_sha256"],
                hashlib.sha256(canonical).hexdigest(),
            )
            self.assertEqual(lines[1]["plan_id"], "plan-1")
            self.assertNotEqual(
                submitted["audit_event_id"],
                accepted["audit_event_id"],
            )

            published = []
            self.assertEqual(
                audit.publish_pending(
                    lambda stream, event: published.append((stream, event)),
                    maximum_events=8,
                ),
                2,
            )
            self.assertEqual(len(published), 2)
            self.assertEqual(audit.status()["pending_count"], 0)

            restored = ControlAuditOutbox(
                root,
                "provider",
                "new-instance",
                "new-boot",
                config,
            )
            self.assertEqual(restored.status()["last_sequence"], 2)
            self.assertEqual(restored.status()["pending_count"], 0)

    def test_oversized_fabric_copy_is_projected_without_changing_local_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "path": str(root / "events.jsonl"),
                "cursor_path": str(root / "cursor.json"),
                "strict_local_write": True,
                "maximum_fabric_event_bytes": 4096,
            }
            audit = ControlAuditOutbox(
                root,
                "provider",
                "instance",
                "boot",
                config,
            )
            request = {"target": {"position_m": [0.1, 0.2, 0.3]}}
            large_result = {
                "candidate_evaluations": "x" * 12000,
                "plan_id": "plan-large",
            }
            recorded = audit.record(
                lifecycle="ACCEPTED",
                endpoint="/v1/motion/path-plan",
                command_id="command-large",
                canonical_request=request,
                result=large_result,
                plan_id="plan-large",
            )
            local_event = json.loads(
                (root / "events.jsonl").read_text(encoding="utf-8").strip()
            )
            published: list[dict] = []

            self.assertEqual(
                audit.publish_pending(
                    lambda _stream, event: published.append(event),
                    maximum_events=1,
                ),
                1,
            )

            self.assertEqual(local_event["result"], large_result)
            self.assertEqual(published[0]["audit_event_id"], recorded["audit_event_id"])
            self.assertEqual(published[0]["canonical_request"], request)
            self.assertEqual(
                published[0]["result"]["fabric_projection"],
                "OVERSIZED_PROVIDER_LOCAL_RESULT",
            )
            self.assertTrue(
                published[0]["fabric_projection"]["exact_provider_local_record"]
            )
            self.assertLess(
                len(
                    json.dumps(
                        published[0],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                4096,
            )
            status = audit.status()
            self.assertEqual(status["pending_count"], 0)
            self.assertEqual(status["projected_fabric_count"], 1)
            self.assertEqual(status["published_sequence"], 1)

    def test_recent_timeline_includes_exact_request_and_fabric_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = ControlAuditOutbox(
                root,
                "provider",
                "instance",
                "boot",
                {
                    "path": str(root / "events.jsonl"),
                    "cursor_path": str(root / "cursor.json"),
                    "strict_local_write": True,
                },
            )
            request = {
                "command_id": "command-1",
                "target": [0.0, 0.0, 0.03],
            }
            audit.record(
                lifecycle="SUBMITTED",
                endpoint="/v1/motion/path-plan",
                command_id="command-1",
                canonical_request=request,
            )

            timeline = audit.recent_events(limit=10)

            self.assertEqual(timeline["status"], "OK")
            self.assertTrue(timeline["exact_canonical_request_included"])
            self.assertEqual(
                timeline["events"][-1]["canonical_request"],
                request,
            )
            self.assertEqual(
                timeline["events"][-1]["fabric_delivery"],
                "PENDING",
            )

    def test_best_effort_local_write_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = ControlAuditOutbox(
                root,
                "provider",
                "instance",
                "boot",
                {
                    "path": str(root),
                    "cursor_path": str(root / "cursor.json"),
                    "strict_local_write": False,
                },
            )

            event = audit.record(
                lifecycle="SUBMITTED",
                endpoint="/v1/engage",
                command_id="command-1",
                canonical_request={"enabled": True},
            )

            self.assertFalse(event["local_delivery"]["persisted"])
            self.assertTrue(event["local_delivery"]["error"])
            self.assertEqual(audit.status()["local_write_failure_count"], 1)
            self.assertEqual(audit.status()["pending_count"], 1)

    def test_fabric_failure_keeps_local_event_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = ControlAuditOutbox(
                root,
                "provider",
                "instance",
                "boot",
                {
                    "path": str(root / "events.jsonl"),
                    "cursor_path": str(root / "cursor.json"),
                },
            )
            audit.record(
                lifecycle="SUBMITTED",
                endpoint="/v1/engage",
                command_id="command-1",
                canonical_request={"enabled": True},
            )

            def fail(_stream, _event):
                raise RuntimeError("Fabric offline")

            self.assertEqual(audit.publish_pending(fail), 0)
            self.assertEqual(audit.status()["pending_count"], 1)
            self.assertIn("Fabric offline", audit.status()["last_fabric_error"])

    def test_signed_path_plan_is_nonphysical_and_returns_normalized_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                None,
                None,
            )
            spatial_resolution = {"reference_frame": "ARM_BASE"}
            spatial_sha256 = hashlib.sha256(
                json.dumps(
                    spatial_resolution,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            request = {
                "command_id": "command-1",
                "related_skill_id": "skill-1",
                "target": {"position_m": [0.1, 0.2, 0.3]},
                "request_context": {
                    "context_kind": "AUTONOMOUS_FREE_SPACE_KINEMATIC",
                    "spatial_resolution_sha256": spatial_sha256,
                    "spatial_resolution_resolved_at_us": time.time_ns() // 1000,
                    "spatial_resolution": spatial_resolution,
                },
            }

            result = service._audited_control(
                "/v1/motion/path-plan",
                request,
                lambda: service._direct_plan_transit_path(request),
            )

            self.assertEqual(result["status"], "PLANNED")
            self.assertEqual(result["enforcement"], "SHADOW_NONPHYSICAL")
            self.assertFalse(result["physical_motion_authorized"])
            self.assertEqual(result["plan_id"], "transit-plan-1")
            self.assertEqual(
                result["target_position_m"],
                [0.1, 0.2, 0.3],
            )
            self.assertEqual(result["control_audit"]["command_id"], "command-1")
            self.assertTrue(result["control_audit"]["submitted_local_persisted"])
            self.assertTrue(result["control_audit"]["accepted_local_persisted"])
            self.assertIsNone(result["control_audit"]["post_action_audit_error"])

    def test_post_action_audit_failure_does_not_rewrite_accepted_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                        "strict_local_write": True,
                    },
                },
                None,
                None,
            )
            submitted = {
                "audit_event_id": "submitted-1",
                "local_delivery": {"persisted": True},
            }
            operation_calls = 0

            def operation():
                nonlocal operation_calls
                operation_calls += 1
                return {"status": "accepted"}

            with patch.object(
                service.control_audit,
                "record",
                side_effect=[submitted, OSError("accepted audit disk failure")],
            ):
                result = service._audited_control(
                    "/v1/engage",
                    {"enabled": True},
                    operation,
                )

            self.assertEqual(operation_calls, 1)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                result["control_audit"]["submitted_event_id"],
                "submitted-1",
            )
            self.assertIsNone(result["control_audit"]["accepted_event_id"])
            self.assertFalse(result["control_audit"]["accepted_local_persisted"])
            self.assertIn(
                "accepted audit disk failure",
                result["control_audit"]["post_action_audit_error"],
            )

    def test_pre_action_strict_audit_failure_prevents_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                        "strict_local_write": True,
                    },
                },
                None,
                None,
            )
            operation_calls = 0

            def operation():
                nonlocal operation_calls
                operation_calls += 1
                return {"status": "accepted"}

            with patch.object(
                service.control_audit,
                "record",
                side_effect=OSError("submitted audit disk failure"),
            ):
                with self.assertRaisesRegex(OSError, "submitted audit disk failure"):
                    service._audited_control(
                        "/v1/engage",
                        {"enabled": True},
                        operation,
                    )

            self.assertEqual(operation_calls, 0)

    def test_rejected_operation_preserves_original_error_and_audits_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                        "strict_local_write": True,
                    },
                },
                None,
                None,
            )

            def rejected_operation():
                raise ValueError("controller rejected the request")

            with self.assertRaisesRegex(
                ValueError,
                "controller rejected the request",
            ):
                service._audited_control(
                    "/v1/motion/path-plan",
                    {"command_id": "rejected-command"},
                    rejected_operation,
                )

            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [event["lifecycle"] for event in events],
                ["SUBMITTED", "REJECTED"],
            )
            self.assertEqual(
                events[-1]["error"],
                "controller rejected the request",
            )

    def test_manager_authority_is_observed_without_replacing_local_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "manager_authority": {
                        "enabled": True,
                        "mode": "SHADOW_OBSERVE",
                        "resource_id": "robot_arm.primary",
                    },
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                "http://manager",
                None,
            )
            service.platform.control_authority = lambda _resource_id: {  # type: ignore[method-assign]
                "resource_id": "robot_arm.primary",
                "enforcement": "ADVISORY",
                "active_lease": {
                    "lease_id": "manager-lease",
                    "owner_id": "skill-1",
                    "fencing_generation": 3,
                },
            }

            local_lease = {
                "active": True,
                "lease_id": "basic-lease",
                "fencing_generation": 9,
            }
            service._poll_advisory_authority(
                {"lease": local_lease, "engaged": True}
            )

            status = service.manager_authority_status
            self.assertEqual(
                status["comparison"],
                "DUAL_LAYER_UNCORRELATED",
            )
            self.assertIn(
                "AUTHORITY_LINEAGE_NOT_BOUND",
                status["evaluation"]["disagreement_reasons"],
            )
            self.assertEqual(status["manager_view"]["active_lease"]["lease_id"], "manager-lease")
            self.assertEqual(status["local_basic_lease"]["lease_id"], "basic-lease")
            self.assertEqual(status["enforcement"], "ADVISORY")
            self.assertFalse(status["physical_enforcement"])
            self.assertFalse(status["may_replace_local_basic_lease"])
            self.assertFalse(status["may_switch_control_mode"])
            self.assertFalse(status["may_submit_motor_commands"])
            self.assertEqual(service.controller.physical_control_call_count, 0)

    def test_manager_authority_resolves_assembly_arm_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "manager_authority": {
                        "enabled": True,
                        "mode": "SHADOW_OBSERVE",
                        "resource_id": "ASSEMBLY_ARM_GROUP",
                    },
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                "http://manager",
                None,
            )
            observed_resource_ids = []

            def observe(resource_id):
                observed_resource_ids.append(resource_id)
                return {
                    "resource_id": resource_id,
                    "enforcement": "ADVISORY",
                    "active_lease": None,
                }

            service.platform.control_authority = observe  # type: ignore[method-assign]
            service._poll_advisory_authority(
                {
                    "arm_resource_id": "robot_arm.primary/arm",
                    "lease": None,
                    "engaged": False,
                }
            )

            self.assertEqual(observed_resource_ids, ["robot_arm.primary/arm"])
            self.assertEqual(
                service.manager_authority_status["configured_resource_id"],
                "ASSEMBLY_ARM_GROUP",
            )
            self.assertEqual(
                service.manager_authority_status["resource_id"],
                "robot_arm.primary/arm",
            )

    def test_transit_path_plan_is_controller_owned_shadow_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                None,
                None,
            )
            request = {
                "command_id": "path-command-1",
                "related_skill_id": "skill-1",
                "target": {
                    "position_m": [0.2, 0.1, 0.4],
                    "rpy_rad": [0.0, 0.0, 0.0],
                },
                "requested_speed_m_s": 0.4,
                "request_context": {
                    "binding_id": "binding-1",
                    "camera_provider_id": "camera.test",
                    "camera_provider_instance_id": "camera-instance-1",
                    "camera_boot_id": "camera-boot-1",
                    "workcell_transform_id": "transform-1",
                    "workcell_transform_revision": "transform-revision-1",
                    "workcell_transform_validity_policy": (
                        "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                    ),
                    "vio_session_epoch": "vio-epoch-1",
                    "observation_timestamp_us": 1,
                    "observation_expires_at_us": 9_999_999_999_999_999,
                    "scene_revision": "scene-1",
                },
            }

            result = service._audited_control(
                "/v1/motion/path-plan",
                request,
                lambda: service._direct_plan_transit_path(request),
            )

            self.assertEqual(
                result["planner_owner"],
                "ROBOT_ARM_INTEGRATED_CONTROLLER",
            )
            self.assertEqual(result["enforcement"], "SHADOW_NONPHYSICAL")
            self.assertFalse(result["physical_motion_authorized"])
            contract = result["preview_contract"]
            self.assertTrue(contract["request_context_complete"])
            self.assertEqual(contract["request_context_issues"], [])
            self.assertIsNone(contract["scene_revision_adaptation"])
            self.assertEqual(
                contract["preview_id"],
                "transit-plan-1",
            )
            self.assertEqual(
                contract["controller_provider_id"],
                "robot_arm.primary.integrated",
            )
            self.assertEqual(
                len(contract["controller_configuration_sha256"]),
                64,
            )
            self.assertFalse(contract["preview_grants_commit_authority"])
            self.assertEqual(
                contract["request_sha256"],
                hashlib.sha256(
                    json.dumps(
                        contract["normalized_request"],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(result["control_audit"]["command_id"], "path-command-1")
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(events[0]["canonical_request"], request)

    def test_transit_preview_uses_newest_controller_scene_revision(self) -> None:
        service = IntegratedService(
            _Controller(),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        now_us = time.time_ns() // 1000
        request = {
            "target": {"position_m": [0.2, 0.1, 0.4], "rpy_rad": None},
            "requested_speed_m_s": 0.05,
            "request_context": {
                "binding_id": "binding-1",
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "camera-instance-1",
                "camera_boot_id": "camera-boot-1",
                "workcell_transform_id": "transform-1",
                "workcell_transform_revision": "transform-revision-1",
                "workcell_transform_validity_policy": (
                    "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                ),
                "vio_session_epoch": "vio-epoch-1",
                "observation_timestamp_us": now_us,
                "observation_expires_at_us": now_us + 60_000_000,
                "scene_revision": "scene-older",
            },
        }

        result = service._direct_plan_transit_path(request)
        contract = result["preview_contract"]

        self.assertTrue(contract["request_context_complete"])
        self.assertNotIn(
            "SCENE_REVISION_MISMATCH",
            contract["request_context_issues"],
        )
        self.assertEqual(contract["scene_revision"], "scene-1")
        self.assertEqual(
            contract["scene_revision_adaptation"]["policy"],
            "CONTROLLER_NEWEST_ACCEPTED_SCENE_USED",
        )

    def test_transit_preview_allows_missing_scene_revision(self) -> None:
        service = IntegratedService(
            _Controller(scene_revision=None),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        now_us = time.time_ns() // 1000
        request = {
            "target": {"position_m": [0.2, 0.1, 0.4], "rpy_rad": None},
            "requested_speed_m_s": 0.05,
            "request_context": {
                "binding_id": "binding-1",
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "camera-instance-1",
                "camera_boot_id": "camera-boot-1",
                "workcell_transform_id": "transform-1",
                "workcell_transform_revision": "transform-revision-1",
                "workcell_transform_validity_policy": (
                    "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                ),
                "vio_session_epoch": "vio-epoch-1",
                "observation_timestamp_us": now_us,
                "observation_expires_at_us": now_us + 60_000_000,
            },
        }

        result = service._direct_plan_transit_path(request)
        contract = result["preview_contract"]

        self.assertTrue(contract["request_context_complete"])
        self.assertEqual(contract["request_context_issues"], [])
        self.assertIsNone(contract["scene_revision"])
        self.assertIsNone(contract["scene_revision_adaptation"])

    def test_autonomous_free_space_context_is_digest_bound_without_ui(self) -> None:
        service = IntegratedService(
            _Controller(scene_revision=None),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        spatial_resolution = {
            "schema": "physical_agent.semantic_direction_resolution",
            "schema_version": 2,
            "direction": "ARM_BASE_POSITIVE_Z",
            "reference_frame": "ARM_BASE",
            "resolved_unit_vector": [0.0, 0.0, 1.0],
            "provenance": {
                "resolution_source": "EXPLICIT_ARM_BASE_AXIS",
                "resolved_at_us": time.time_ns() // 1000,
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                spatial_resolution,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = {
            "target": {"position_m": [0.2, 0.1, 0.4], "rpy_rad": None},
            "requested_speed_m_s": 0.05,
            "request_context": {
                "context_kind": "AUTONOMOUS_FREE_SPACE_KINEMATIC",
                "spatial_resolution_sha256": digest,
                "spatial_resolution_resolved_at_us": (
                    spatial_resolution["provenance"]["resolved_at_us"]
                ),
                "spatial_resolution": spatial_resolution,
            },
        }

        result = service._direct_plan_transit_path(request)

        self.assertTrue(
            result["preview_contract"]["request_context_complete"]
        )
        self.assertEqual(
            result["preview_contract"]["request_context_issues"],
            [],
        )
        request["request_context"]["spatial_resolution_sha256"] = "bad"
        rejected = service._direct_plan_transit_path(request)
        self.assertFalse(
            rejected["preview_contract"]["request_context_complete"]
        )
        self.assertIn(
            "SPATIAL_RESOLUTION_DIGEST_MISMATCH",
            rejected["preview_contract"]["request_context_issues"],
        )

    def test_transit_preview_preserves_relative_delta_request(self) -> None:
        service = IntegratedService(
            _Controller(),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        request = {
            "target": {
                "position_delta_m": [0.05, 0.0, 0.0],
                "rpy_rad": None,
            },
            "requested_speed_m_s": 0.03,
        }

        result = service._direct_plan_transit_path(request)

        self.assertEqual(
            result["preview_contract"]["normalized_request"]["target"][
                "position_delta_m"
            ],
            [0.05, 0.0, 0.0],
        )
        self.assertEqual(
            result["target_delta_m"],
            [0.05, 0.0, 0.0],
        )

    def test_transit_backend_defaults_to_impedance_and_pos_speed_is_signed(
        self,
    ) -> None:
        service = IntegratedService(
            _Controller(),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        base_request = {
            "target": {
                "position_delta_m": [0.01, 0.0, 0.0],
                "rpy_rad": None,
            },
            "requested_speed_m_s": 0.03,
        }

        default_result = service._direct_plan_transit_path(base_request)
        selected_result = service._direct_plan_transit_path(
            {**base_request, "execution_backend": "pos_speed"}
        )

        self.assertEqual(
            default_result["preview_contract"]["normalized_request"][
                "execution_backend"
            ],
            "IMPEDANCE",
        )
        self.assertEqual(
            selected_result["preview_contract"]["normalized_request"][
                "execution_backend"
            ],
            "POS_SPEED",
        )
        self.assertNotEqual(
            default_result["preview_contract"]["request_sha256"],
            selected_result["preview_contract"]["request_sha256"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "execution_backend must be IMPEDANCE or POS_SPEED",
        ):
            service._direct_plan_transit_path(
                {**base_request, "execution_backend": "POS_TOR"}
            )

    def test_v2_mounted_activation_survives_camera_and_vio_process_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = IntegratedService(
                _Controller(),  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                None,
                None,
            )
            context = {
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "current-instance",
                "camera_boot_id": "current-boot",
                "camera_calibration_revision": "camera-calibration-1",
                "workcell_transform_id": "transform-1",
                "workcell_transform_revision": "transform-revision-1",
                "workcell_transform_validity_policy": (
                    "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
                ),
            }
            service.platform.workcell_calibrations = lambda: {
                "enforcement": "ENFORCED",
                "activations": [
                    {
                        "activation_id": "activation-1",
                        "candidate_id": "transform-1",
                        "calibration_revision": "transform-revision-1",
                        "state": "ACTIVE",
                        "motion_usable": True,
                        "expires_at_us": None,
                        "validity_policy": (
                            "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
                        ),
                        "camera_provider_id": "camera.test",
                        "camera_provider_instance_id": "prior-instance",
                        "camera_boot_id": "prior-boot",
                        "camera_calibration_revision": "camera-calibration-1",
                        "session_epoch": "historical-vio-epoch",
                    }
                ],
            }

            activation = service._require_current_workcell_activation(
                context,
                now_us=time.time_ns() // 1000,
            )

            self.assertEqual(activation["activation_id"], "activation-1")

    def test_v2_transit_preview_does_not_require_vio_epoch(self) -> None:
        service = IntegratedService(
            _Controller(),  # type: ignore[arg-type]
            {
                "provider_id": "robot_arm.primary.integrated",
                "listen_host": "127.0.0.1",
                "listen_port": 8793,
            },
            None,
            None,
        )
        now_us = time.time_ns() // 1000
        request = {
            "target": {
                "position_delta_m": [0.05, 0.0, 0.0],
                "rpy_rad": None,
            },
            "requested_speed_m_s": 0.03,
            "request_context": {
                "binding_id": "binding-1",
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "camera-instance-1",
                "camera_boot_id": "camera-boot-1",
                "camera_calibration_revision": "camera-calibration-1",
                "workcell_transform_id": "transform-1",
                "workcell_transform_revision": "transform-revision-1",
                "workcell_transform_validity_policy": (
                    "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
                ),
                "observation_timestamp_us": now_us,
                "observation_expires_at_us": now_us + 60_000_000,
                "scene_revision": "scene-1",
            },
        }

        preview = service._direct_plan_transit_path(request)

        contract = preview["preview_contract"]
        self.assertTrue(contract["request_context_complete"])
        self.assertNotIn(
            "MISSING_REQUEST_CONTEXT:vio_session_epoch",
            contract["request_context_issues"],
        )

    def test_signed_transit_commit_is_exact_one_time_and_audited_by_hash(
        self,
    ) -> None:
        secret = "a" * 32
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {"MIDBRAIN_AUTHORIZATION_SECRET": secret},
        ):
            root = Path(temporary)
            controller = _Controller()
            service = IntegratedService(
                controller,  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                None,
                None,
            )
            now_us = time.time_ns() // 1000
            request = {
                "target": {
                    "position_m": [0.2, 0.1, 0.4],
                    "rpy_rad": None,
                },
                "requested_speed_m_s": 0.05,
                "request_context": {
                    "binding_id": "binding-1",
                    "camera_provider_id": "camera.test",
                    "camera_provider_instance_id": "camera-instance-1",
                    "camera_boot_id": "camera-boot-1",
                    "workcell_transform_id": "transform-1",
                    "workcell_transform_revision": "transform-revision-1",
                    "workcell_transform_validity_policy": (
                        "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                    ),
                    "vio_session_epoch": "vio-epoch-1",
                    "observation_timestamp_us": now_us,
                    "observation_expires_at_us": now_us + 60_000_000,
                    "scene_revision": "scene-1",
                },
            }
            preview = service._direct_plan_transit_path(request)
            contract = preview["preview_contract"]
            service.platform.workcell_calibrations = lambda: {
                "enforcement": "ENFORCED",
                "activations": [
                    {
                        "activation_id": "activation-1",
                        "candidate_id": "transform-1",
                        "calibration_revision": "transform-revision-1",
                        "state": "ACTIVE",
                        "motion_usable": True,
                        "expires_at_us": None,
                        "validity_policy": (
                            "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                        ),
                        "camera_provider_id": "camera.test",
                        "camera_provider_instance_id": (
                            "camera-instance-1"
                        ),
                        "camera_boot_id": "camera-boot-1",
                        "session_epoch": "vio-epoch-1",
                    }
                ],
            }
            claims = {
                "schema": (
                    "physical_agent.authorization_execution_assertion"
                ),
                "schema_version": 1,
                "assertion_id": "assertion-1",
                "issuer": "physical-agent-ui",
                "audience": "robot_arm.primary.integrated",
                "action": "EXECUTE_TRANSIT_PATH",
                "decision_id": "decision-1",
                "decision_type": "PHYSICAL_OBSERVATION_POSE",
                "resolution": "APPROVED",
                "resolved_by": "operator",
                "issued_at_us": now_us,
                "expires_at_us": contract["expires_at_us"],
                "controller_provider_id": (
                    "robot_arm.primary.integrated"
                ),
                "controller_provider_instance_id": (
                    service.platform.instance_id
                ),
                "controller_boot_id": service.platform.boot_id,
                "controller_configuration_sha256": contract[
                    "controller_configuration_sha256"
                ],
                "plan_id": contract["preview_id"],
                "request_sha256": contract["request_sha256"],
                "preview_sha256": contract["preview_sha256"],
                "scene_revision": contract["scene_revision"],
            }
            payload = base64.urlsafe_b64encode(
                json.dumps(
                    claims,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).rstrip(b"=").decode("ascii")
            signature = base64.urlsafe_b64encode(
                hmac.new(
                    secret.encode("utf-8"),
                    payload.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            ).rstrip(b"=").decode("ascii")
            assertion = f"{payload}.{signature}"
            commit_request = {
                "plan_id": contract["preview_id"],
                "request_sha256": contract["request_sha256"],
                "preview_sha256": contract["preview_sha256"],
                "authorization_assertion_sha256": hashlib.sha256(
                    assertion.encode("ascii")
                ).hexdigest(),
            }
            result = service._audited_control(
                "/v1/motion/path-commit",
                commit_request,
                lambda: service._direct_commit_transit_path(
                    commit_request,
                    assertion,
                ),
            )

            self.assertTrue(result["physical_motion_authorized"])
            self.assertEqual(controller.physical_control_call_count, 1)
            with self.assertRaisesRegex(
                PermissionError,
                "already consumed|state is",
            ):
                service._direct_commit_transit_path(
                    commit_request,
                    assertion,
                )
            audit_text = (root / "events.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(assertion, audit_text)
            self.assertIn(
                commit_request["authorization_assertion_sha256"],
                audit_text,
            )

    def test_transit_preview_expires_with_its_observation_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = _Controller()
            service = IntegratedService(
                controller,  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "planning": {"transit_preview_ttl_ms": 30_000},
                    "control_audit": {
                        "path": str(Path(temporary) / "events.jsonl"),
                        "cursor_path": str(
                            Path(temporary) / "cursor.json"
                        ),
                    },
                },
                None,
                None,
            )
            now_us = time.time_ns() // 1000
            observation_expires_at_us = now_us + 2_000_000
            preview = service._direct_plan_transit_path(
                {
                    "target": {
                        "position_m": [0.2, 0.1, 0.4],
                        "rpy_rad": None,
                    },
                    "request_context": {
                        "binding_id": "binding-1",
                        "camera_provider_id": "camera.test",
                        "camera_provider_instance_id": (
                            "camera-instance-1"
                        ),
                        "camera_boot_id": "camera-boot-1",
                        "workcell_transform_id": "transform-1",
                        "workcell_transform_revision": "revision-1",
                        "workcell_transform_validity_policy": (
                            "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                        ),
                        "vio_session_epoch": "vio-epoch-1",
                        "observation_timestamp_us": now_us,
                        "observation_expires_at_us": (
                            observation_expires_at_us
                        ),
                        "scene_revision": "scene-1",
                    },
                }
            )
            self.assertEqual(
                preview["preview_contract"]["expires_at_us"],
                observation_expires_at_us,
            )

    def test_revoked_workcell_activation_blocks_transit_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = _Controller()
            service = IntegratedService(
                controller,  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "control_audit": {
                        "path": str(Path(temporary) / "events.jsonl"),
                        "cursor_path": str(
                            Path(temporary) / "cursor.json"
                        ),
                    },
                },
                "http://manager",
                None,
            )
            context = {
                "workcell_transform_id": "transform-1",
                "workcell_transform_revision": "revision-1",
                "workcell_transform_validity_policy": (
                    "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                ),
                "camera_provider_id": "camera.test",
                "camera_provider_instance_id": "camera-instance-1",
                "camera_boot_id": "camera-boot-1",
                "vio_session_epoch": "vio-epoch-1",
            }
            service.platform.workcell_calibrations = lambda: {
                "activations": [
                    {
                        "activation_id": "activation-1",
                        "candidate_id": "transform-1",
                        "calibration_revision": "revision-1",
                        "state": "REVOKED",
                        "motion_usable": False,
                        "expires_at_us": None,
                        "validity_policy": (
                            "MOUNTED_IDENTITY_TRACKING_GATED_V1"
                        ),
                        "camera_provider_id": "camera.test",
                        "camera_provider_instance_id": (
                            "camera-instance-1"
                        ),
                        "camera_boot_id": "camera-boot-1",
                        "session_epoch": "vio-epoch-1",
                    }
                ],
            }
            with self.assertRaisesRegex(
                PermissionError,
                "revoked, suspended, invalidated, or changed",
            ):
                service._require_current_workcell_activation(
                    context,
                    now_us=time.time_ns() // 1000,
                )
            self.assertEqual(controller.physical_control_call_count, 0)

    def test_manager_authority_failure_remains_observational(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = _Controller()
            service = IntegratedService(
                controller,  # type: ignore[arg-type]
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "listen_host": "127.0.0.1",
                    "listen_port": 8793,
                    "manager_authority": {
                        "enabled": True,
                        "mode": "SHADOW_OBSERVE",
                        "resource_id": "robot_arm.primary",
                    },
                    "control_audit": {
                        "path": str(root / "events.jsonl"),
                        "cursor_path": str(root / "cursor.json"),
                    },
                },
                "http://manager",
                None,
            )

            def fail(_resource_id):
                raise RuntimeError("Manager unavailable")

            service.platform.control_authority = fail  # type: ignore[method-assign]
            local_lease = {
                "active": True,
                "lease_id": "basic-lease",
                "fencing_generation": 9,
            }
            service._poll_advisory_authority(
                {"lease": local_lease, "engaged": True}
            )

            status = service.manager_authority_status
            self.assertEqual(
                status["comparison"],
                "MANAGER_UNAVAILABLE_WITH_LOCAL_LEASE",
            )
            self.assertEqual(
                status["metrics"]["disagreement_counts"][
                    "MANAGER_UNAVAILABLE"
                ],
                1,
            )
            self.assertEqual(status["local_basic_lease"], local_lease)
            self.assertFalse(status["physical_enforcement"])
            self.assertEqual(controller.physical_control_call_count, 0)


if __name__ == "__main__":
    unittest.main()
