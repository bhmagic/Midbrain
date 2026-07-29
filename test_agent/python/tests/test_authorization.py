from __future__ import annotations

import unittest

from physical_agent_test.authorization import AuthorizationStore
from rebot_arm_integrated.authorization import (
    verify_transit_execution_assertion,
)


class AuthorizationStoreTests(unittest.TestCase):
    def test_decision_is_specific_and_approval_never_executes_action(self) -> None:
        store = AuthorizationStore()
        created = store.create(
            requester_type="skill",
            requester_id="inspect-pointed-object-1",
            decision_type="ROBOT_MOTION",
            title="Move above the pointed object",
            summary="Position the effector 10 cm above the selected object.",
            proposed_action={
                "provider_id": "robot_arm.primary.integrated",
                "plan_id": "plan-1",
                "target_position_m": [0.2, 0.1, 0.3],
            },
            evidence={
                "frame_id": "frame-1",
                "confidence": "medium",
            },
            safety={
                "requires_physical_authority": True,
            },
        )

        self.assertEqual(created["status"], "PENDING")
        self.assertFalse(created["safety"]["approval_executes_action"])
        resolved = store.resolve(
            created["decision_id"],
            resolution="APPROVED",
            resolved_by="operator",
        )
        self.assertEqual(resolved["status"], "APPROVED")
        self.assertFalse(resolved["safety"]["approval_executes_action"])

    def test_decision_cannot_be_resolved_twice(self) -> None:
        store = AuthorizationStore()
        created = store.create(
            requester_type="provider",
            requester_id="provider-1",
            decision_type="MODE_CHANGE",
            title="Change mode",
            summary="Request mode transition.",
            proposed_action={},
            evidence={},
            safety={},
        )
        store.resolve(
            created["decision_id"],
            resolution="DENIED",
            resolved_by="operator",
        )

        with self.assertRaisesRegex(RuntimeError, "already DENIED"):
            store.resolve(
                created["decision_id"],
                resolution="APPROVED",
                resolved_by="operator",
            )

    def test_approved_preview_mints_one_exact_signed_execution_assertion(
        self,
    ) -> None:
        secret = "s" * 32
        store = AuthorizationStore(signing_secret=secret)
        authority = {
            "plan_id": "plan-1",
            "request_sha256": "request-sha",
            "preview_sha256": "preview-sha",
            "controller_provider_id": "robot_arm.primary.integrated",
            "controller_provider_instance_id": "instance-1",
            "controller_boot_id": "boot-1",
            "controller_configuration_sha256": "config-sha",
            "issued_at_us": 1,
            "expires_at_us": 10**18,
            "scene_revision": "scene-1",
            "lease_snapshot": {},
        }
        created = store.create(
            requester_type="skill",
            requester_id="observe-object",
            decision_type="PHYSICAL_OBSERVATION_POSE",
            title="Move above object",
            summary="Move to the reviewed observation pose.",
            proposed_action={"plan_id": "plan-1"},
            evidence={},
            safety={"controller_preview_authority": authority},
            expires_in_s=120.0,
        )
        store.resolve(
            created["decision_id"],
            resolution="APPROVED",
            resolved_by="operator",
        )

        issued = store.issue_execution_assertion(created["decision_id"])
        claims = verify_transit_execution_assertion(
            issued["assertion"],
            secret,
            provider_id="robot_arm.primary.integrated",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            configuration_sha256="config-sha",
            plan_id="plan-1",
            request_sha256="request-sha",
            preview_sha256="preview-sha",
            scene_revision="scene-1",
            preview_expires_at_us=10**18,
        )

        self.assertEqual(claims["resolved_by"], "operator")
        self.assertFalse(issued["approval_executes_action"])
        with self.assertRaisesRegex(RuntimeError, "already issued"):
            store.issue_execution_assertion(created["decision_id"])


if __name__ == "__main__":
    unittest.main()
