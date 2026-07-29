from __future__ import annotations

import unittest

from physical_agent_test.reviewed_observation_execution import (
    ReviewedObservationExecutionAdapter,
)


def approved_record() -> dict:
    return {
        "decision_type": "PHYSICAL_OBSERVATION_POSE",
        "status": "APPROVED",
        "evidence": {
            "semantic_scene": {
                "scene_revision": "scene-1",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
        },
        "safety": {
            "approval_executes_action": False,
            "controller_preview_authority": {
                "plan_id": "plan-1",
                "request_sha256": "request-sha",
                "preview_sha256": "preview-sha",
                "controller_provider_id": "robot_arm.primary.integrated",
                "controller_provider_instance_id": "instance-1",
                "controller_boot_id": "boot-1",
                "controller_configuration_sha256": "config-sha",
                "scene_revision": "scene-1",
                "expires_at_us": 9_999_999_999_999_999,
            },
        },
    }


class Store:
    def __init__(self, record: dict):
        self.record = record
        self.issued_for: str | None = None

    def get(self, _decision_id: str) -> dict:
        return self.record

    def issue_execution_assertion(self, decision_id: str) -> dict:
        self.issued_for = decision_id
        return {
            "assertion": "signed-assertion",
            "assertion_sha256": "assertion-sha",
        }


class Integrated:
    def __init__(self):
        self.payload: dict | None = None
        self.assertion: str | None = None
        self.scene: dict | None = None

    async def stage_scene(self, payload: dict) -> dict:
        self.scene = payload
        return {
            "accepted": True,
            "scene": {"revision": payload["scene_revision"]},
        }

    async def commit_transit_path(
        self,
        payload: dict,
        *,
        authorization_assertion: str,
    ) -> dict:
        self.payload = payload
        self.assertion = authorization_assertion
        return {"status": "COMPLETED", "commit_id": "commit-1"}


class ReviewedObservationExecutionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_commits_only_recorded_preview_identity(self) -> None:
        store = Store(approved_record())
        integrated = Integrated()
        adapter = ReviewedObservationExecutionAdapter(store, integrated)

        result = await adapter.run(decision_id="decision-1")

        self.assertEqual(store.issued_for, "decision-1")
        self.assertEqual(
            integrated.scene,
            {
                "scene_revision": "scene-1",
                "frame_id": "rebot_arm_base",
                "spheres": [],
            },
        )
        self.assertEqual(
            integrated.payload,
            {
                "plan_id": "plan-1",
                "request_sha256": "request-sha",
                "preview_sha256": "preview-sha",
                "decision_id": "decision-1",
                "authorization_assertion_sha256": "assertion-sha",
            },
        )
        self.assertEqual(integrated.assertion, "signed-assertion")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(result["model_supplied_motion_parameters"])

    async def test_rejects_nonapproved_decision_before_assertion(
        self,
    ) -> None:
        record = approved_record()
        record["status"] = "PENDING"
        store = Store(record)
        adapter = ReviewedObservationExecutionAdapter(store, Integrated())

        with self.assertRaisesRegex(RuntimeError, "APPROVED"):
            await adapter.run(decision_id="decision-1")

        self.assertIsNone(store.issued_for)

    async def test_rejects_scene_revision_mismatch_before_assertion(
        self,
    ) -> None:
        record = approved_record()
        record["evidence"]["semantic_scene"]["scene_revision"] = "scene-2"
        store = Store(record)
        adapter = ReviewedObservationExecutionAdapter(store, Integrated())

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            await adapter.run(decision_id="decision-1")

        self.assertIsNone(store.issued_for)


if __name__ == "__main__":
    unittest.main()
