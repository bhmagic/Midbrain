from __future__ import annotations

import copy
import hashlib
import json
import time
import unittest

import numpy as np

from physical_agent_test.authorization import AuthorizationStore
from physical_agent_test.observation_motion import (
    attach_controller_preview,
    build_observation_motion_proposal,
    create_observation_motion_authorization,
)


class ObservationMotionTests(unittest.TestCase):
    @staticmethod
    def _preview_context(now_us: int | None = None) -> dict:
        now = time.time_ns() // 1000 if now_us is None else int(now_us)
        return {
            "binding_id": "binding-1",
            "camera_provider_id": "camera.test",
            "camera_provider_instance_id": "camera-instance-1",
            "camera_boot_id": "camera-boot-1",
            "workcell_transform_id": "transform-1",
            "workcell_transform_revision": "transform-revision-1",
            "workcell_transform_expires_at_us": now + 60_000_000,
            "vio_session_epoch": "vio-epoch-1",
            "observation_timestamp_us": now - 10_000,
            "observation_expires_at_us": now + 5_000_000,
            "scene_revision": "scene-1",
        }

    @staticmethod
    def _canonical_sha256(value: object) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _accepted_preview(
        cls,
        proposal: dict,
        *,
        now_us: int | None = None,
    ) -> dict:
        now = time.time_ns() // 1000 if now_us is None else int(now_us)
        request = proposal["controller_plan_request"]
        normalized_request = {
            "target": copy.deepcopy(request["target"]),
            "requested_speed_m_s": float(request["requested_speed_m_s"]),
            "allowed_contact_object_ids": [],
            "permit_pushable_contact": False,
            "request_context": copy.deepcopy(request["request_context"]),
        }
        preview = {
            "status": "PLANNED",
            "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
            "enforcement": "SHADOW_NONPHYSICAL",
            "physical_motion_authorized": False,
            "control_state_unchanged": True,
            "lease_unchanged": True,
            "plan_id": "preview-1",
            "selected_plan": {
                "planning_valid": True,
                "planning_reasons": [],
                "preview": {
                    "collision_free": True,
                    "preview_id": "preview-1",
                },
            },
            "scene_revision": "scene-1",
        }
        contract = {
            "schema": "physical_agent.integrated_transit_preview_contract",
            "schema_version": 1,
            "preview_id": "preview-1",
            "issued_at_us": now,
            "expires_at_us": now + 5_000_000,
            "ttl_ms": 5000,
            "controller_provider_id": "robot_arm.primary.integrated",
            "controller_provider_instance_id": "controller-instance-1",
            "controller_boot_id": "controller-boot-1",
            "controller_configuration_sha256": "config-sha-1",
            "request_sha256": cls._canonical_sha256(normalized_request),
            "normalized_request": normalized_request,
            "request_context_sha256": cls._canonical_sha256(
                normalized_request["request_context"]
            ),
            "request_context_complete": True,
            "request_context_issues": [],
            "scene_revision": "scene-1",
            "lease_snapshot": {
                "active": False,
                "state": "AVAILABLE",
                "lease_id": None,
                "fencing_generation": None,
            },
            "basic_feedback": {
                "observed_at_us": now,
                "last_applied_command_id": None,
            },
            "physical_motion_authorized": False,
            "preview_grants_commit_authority": False,
            "commit_endpoint_exposed": False,
        }
        contract["preview_sha256"] = cls._canonical_sha256(
            {
                "planning_result": preview,
                "preview_contract": contract,
            }
        )
        preview["preview_contract"] = contract
        return preview

    def _proposal(self) -> dict:
        return build_observation_motion_proposal(
            object_point_world_m=[0.4, 0.2, 0.1],
            world_from_arm_base=np.eye(4),
            view_mode="TOP",
            standoff_m=0.15,
            source_evidence={"vlm_model": "test"},
            preview_context=self._preview_context(),
        )

    def test_top_proposal_is_nonphysical_until_separate_authorization(self) -> None:
        proposal = build_observation_motion_proposal(
            object_point_world_m=[0.4, 0.2, 0.1],
            world_from_arm_base=np.eye(4),
            view_mode="TOP",
            standoff_m=0.15,
            source_evidence={"spatial_registration_id": "point-1"},
            preview_context=self._preview_context(),
        )

        self.assertEqual(proposal["status"], "AUTHORIZATION_REQUIRED")
        self.assertFalse(proposal["motion_usable"])
        self.assertFalse(proposal["physical_motion_authorized"])
        self.assertTrue(
            np.allclose(proposal["proposed_position_world_m"], [0.4, 0.35, 0.1])
        )
        self.assertFalse(
            proposal["controller_plan_request"]["physical_motion_authorized"]
        )

    def test_front_view_approaches_from_arm_base_side(self) -> None:
        proposal = build_observation_motion_proposal(
            object_point_world_m=[0.4, 0.2, 0.0],
            world_from_arm_base=np.eye(4),
            view_mode="FRONT",
            standoff_m=0.1,
            source_evidence={},
            preview_context=self._preview_context(),
        )

        self.assertTrue(
            np.allclose(proposal["proposed_position_world_m"], [0.3, 0.2, 0.0])
        )

    def test_authorization_record_never_executes_the_proposal(self) -> None:
        proposal = self._proposal()
        proposal = attach_controller_preview(
            proposal,
            self._accepted_preview(proposal),
        )

        record = create_observation_motion_authorization(
            AuthorizationStore(),
            proposal,
            requester_id="observe-pointed-object-test",
        )

        self.assertEqual(record["status"], "PENDING")
        self.assertFalse(record["safety"]["approval_executes_action"])
        self.assertTrue(record["safety"]["physical_motion"])
        self.assertFalse(
            record["proposed_action"]["physical_motion_authorized"]
        )
        self.assertEqual(
            record["safety"]["controller_preview_authority"][
                "controller_boot_id"
            ],
            "controller-boot-1",
        )
        self.assertTrue(
            record["safety"]["fresh_fenced_authority_required_at_execution"]
        )
        self.assertLessEqual(
            record["expires_at_us"],
            record["safety"]["controller_preview_authority"][
                "expires_at_us"
            ],
        )

    def test_rejected_controller_preview_is_not_authorization_ready(self) -> None:
        proposal = self._proposal()
        preview = self._accepted_preview(proposal)
        preview["status"] = "REJECTED"

        proposal = attach_controller_preview(proposal, preview)

        self.assertEqual(proposal["status"], "PREVIEW_REJECTED")
        self.assertFalse(proposal["controller_preview_valid"])
        self.assertFalse(proposal["physical_motion_authorized"])
        with self.assertRaisesRegex(ValueError, "preview is required"):
            create_observation_motion_authorization(
                AuthorizationStore(),
                proposal,
                requester_id="observe-pointed-object-test",
            )

    def test_expired_preview_is_rejected(self) -> None:
        now = time.time_ns() // 1000
        proposal = self._proposal()
        preview = self._accepted_preview(proposal, now_us=now - 10_000_000)

        proposal = attach_controller_preview(proposal, preview)

        self.assertFalse(proposal["controller_preview_valid"])
        self.assertIn(
            "PREVIEW_CONTRACT_EXPIRED",
            proposal["controller_preview_validation_issues"],
        )

    def test_tampered_target_is_rejected_by_request_digest(self) -> None:
        proposal = self._proposal()
        preview = self._accepted_preview(proposal)
        proposal["controller_plan_request"]["target"]["position_m"][2] += 0.01

        proposal = attach_controller_preview(proposal, preview)

        self.assertFalse(proposal["controller_preview_valid"])
        self.assertIn(
            "PREVIEW_REQUEST_MISMATCH",
            proposal["controller_preview_validation_issues"],
        )

    def test_controller_boot_tamper_is_rejected_by_preview_digest(self) -> None:
        proposal = self._proposal()
        preview = self._accepted_preview(proposal)
        preview["preview_contract"]["controller_boot_id"] = "restarted-boot"

        proposal = attach_controller_preview(proposal, preview)

        self.assertFalse(proposal["controller_preview_valid"])
        self.assertIn(
            "PREVIEW_DIGEST_MISMATCH",
            proposal["controller_preview_validation_issues"],
        )

    def test_expired_workcell_context_is_rejected(self) -> None:
        proposal = self._proposal()
        preview = self._accepted_preview(proposal)
        expired_at = time.time_ns() // 1000 - 1
        proposal["controller_plan_request"]["request_context"][
            "workcell_transform_expires_at_us"
        ] = expired_at
        preview = self._accepted_preview(proposal)

        proposal = attach_controller_preview(proposal, preview)

        self.assertFalse(proposal["controller_preview_valid"])
        self.assertIn(
            "PREVIEW_CONTEXT_EXPIRED:workcell_transform_expires_at_us",
            proposal["controller_preview_validation_issues"],
        )


if __name__ == "__main__":
    unittest.main()
