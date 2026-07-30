from __future__ import annotations

import copy
import unittest

import httpx

from physical_agent_test.integrated_motion_adapter import (
    IntegratedRelativeMotionAdapter,
)


class _IntegratedClient:
    def __init__(self):
        self.engage_count = 0
        self.trigger_count = 0
        self.completion_success = True
        self.snapshot = {
            "residency": "HOT",
            "health": "HEALTHY",
            "commit_count": 0,
            "model_view": {
                "measured_controlled_frame": {
                    "position_m": [0.1, 0.2, 0.3],
                },
                "staged_controlled_frame": {
                    "position_m": [0.1, 0.2, 0.3],
                },
            },
            "planning": {
                "target_revision": 1,
                "last_preview": None,
            },
            "trajectory": {
                "active": False,
                "last_completed": None,
            },
        }

    async def state(self):
        return copy.deepcopy(self.snapshot)

    async def preview_direct_motion(self, request):
        target = request["command"]["target"]["position_m"]
        self.snapshot["model_view"]["staged_controlled_frame"] = {
            "position_m": list(target)
        }
        self.snapshot["planning"] = {
            "target_revision": 2,
            "last_preview": {
                "preview_id": "preview-1",
                "planning_valid": True,
                "target_revision": 2,
            },
        }
        return {
            "status": "PLANNED",
            "plan_id": "preview-1",
            "preview": {
                "preview_id": "preview-1",
                "planning_valid": True,
            },
        }

    async def engage_staged_motion(self):
        self.engage_count += 1
        return {"status": "engaged_target_edit", "engaged": True}

    async def trigger_one_shot_motion(self):
        self.trigger_count += 1
        self.snapshot["commit_count"] += 1
        target = self.snapshot["model_view"][
            "staged_controlled_frame"
        ]["position_m"]
        self.snapshot["model_view"]["measured_controlled_frame"] = {
            "position_m": (
                list(target)
                if self.completion_success
                else [target[0], target[1] - 0.006, target[2]]
            )
        }
        self.snapshot["trajectory"] = {
            "active": False,
            "last_completed": {
                "completion_success": self.completion_success,
                "completion_outcome": (
                    "ARRIVAL_CONFIRMED_AND_FLOATED"
                    if self.completion_success
                    else "DEADLINE_FLOAT_BEFORE_ARRIVAL"
                ),
            },
        }
        return {
            "accepted": True,
            "physical_motion_authorized": True,
        }


class _OfflineIntegratedClient:
    async def state(self):
        raise httpx.ConnectError("All connection attempts failed")


class IntegratedRelativeMotionAdapterTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_up_preview_uses_positive_arm_base_y(self) -> None:
        client = _IntegratedClient()
        adapter = IntegratedRelativeMotionAdapter(client)

        result = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(result["status"], "PREVIEW_READY")
        self.assertFalse(result["workflow_complete"])
        self.assertEqual(result["target_position_m"], [0.1, 0.4, 0.3])
        self.assertFalse(result["physical_motion_authorized"])
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "execute_integrated_motion_preview",
                "arguments": {
                    "preview_id": "preview-1",
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "UP",
                    "distance_m": 0.2,
                    "original_request_distance_m": 0.2,
                    "target_position_m": [0.1, 0.4, 0.3],
                },
            },
        )
        self.assertEqual(client.engage_count, 0)

    async def test_observation_reports_pending_preview_without_mutation(
        self,
    ) -> None:
        client = _IntegratedClient()
        adapter = IntegratedRelativeMotionAdapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        observation = await adapter.observation()

        self.assertTrue(observation["read_only"])
        self.assertEqual(
            observation["pending_previews"][0]["preview_id"],
            preview["preview_id"],
        )
        self.assertEqual(observation["controller"]["residency"], "HOT")
        self.assertEqual(client.engage_count, 0)
        self.assertEqual(client.trigger_count, 0)

    async def test_unreachable_controller_returns_recovery_route(self) -> None:
        adapter = IntegratedRelativeMotionAdapter(_OfflineIntegratedClient())

        result = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(result["retry_same_tool"])
        self.assertEqual(
            result["required_next_tool"]["name"],
            "inspect_midbrain_runtime",
        )

    async def test_exact_preview_requires_separate_execution(self) -> None:
        client = _IntegratedClient()
        adapter = IntegratedRelativeMotionAdapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        result = await adapter.execute(
            preview_id=preview["preview_id"],
            motion_intent=preview["motion_intent"],
            direction=preview["direction"],
            distance_m=preview["distance_m"],
            original_request_distance_m=preview["distance_m"],
            target_position_m=preview["target_position_m"],
        )

        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertTrue(result["physical_motion_completed"])
        self.assertEqual(client.engage_count, 1)
        self.assertEqual(client.trigger_count, 1)

    async def test_finished_motion_without_arrival_is_not_success(self) -> None:
        client = _IntegratedClient()
        client.completion_success = False
        adapter = IntegratedRelativeMotionAdapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)

        result = await adapter.execute(
            preview_id=preview["preview_id"],
            motion_intent=preview["motion_intent"],
            direction=preview["direction"],
            distance_m=preview["distance_m"],
            original_request_distance_m=preview["distance_m"],
            target_position_m=preview["target_position_m"],
        )

        self.assertEqual(
            result["status"],
            "MOTION_FINISHED_WITHOUT_CONFIRMED_ARRIVAL",
        )
        self.assertFalse(result["physical_motion_completed"])
        self.assertIn("DEADLINE_FLOAT_BEFORE_ARRIVAL", result["message"])

        next_preview = await adapter.preview(direction="UP", distance_m=0.2)

        self.assertEqual(next_preview["motion_intent"], "NEW_RELATIVE_MOVE")
        self.assertAlmostEqual(next_preview["start_position_m"][1], 0.394)
        self.assertAlmostEqual(next_preview["target_position_m"][1], 0.594)

    async def test_changed_preview_is_rejected(self) -> None:
        client = _IntegratedClient()
        adapter = IntegratedRelativeMotionAdapter(client)
        preview = await adapter.preview(direction="UP", distance_m=0.2)
        client.snapshot["planning"]["target_revision"] = 3

        with self.assertRaisesRegex(
            RuntimeError,
            "preview changed before approval",
        ):
            await adapter.execute(
                preview_id=preview["preview_id"],
                motion_intent=preview["motion_intent"],
                direction=preview["direction"],
                distance_m=preview["distance_m"],
                original_request_distance_m=preview["distance_m"],
                target_position_m=preview["target_position_m"],
            )

        self.assertEqual(client.engage_count, 0)
