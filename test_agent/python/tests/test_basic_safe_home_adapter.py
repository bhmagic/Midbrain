from __future__ import annotations

import unittest

from physical_agent_test.basic_safe_home_adapter import BasicSafeHomeAdapter


class _BasicClient:
    def __init__(self, *, state: str = "SAFE_HOLD_GRAVITY_FLOAT"):
        self.provider_state = state
        self.safe_home_count = 0
        self.success = True

    async def state(self):
        return {"provider_state": self.provider_state}

    async def safe_home(self):
        self.safe_home_count += 1
        return {
            "status": "safe_home",
            "success": self.success,
            "details": {
                "active": False,
                "success": self.success,
                "reason": (
                    "safe-home complete"
                    if self.success
                    else "safe-home did not reach stable position"
                ),
            },
        }


class BasicSafeHomeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_only_controller_confirmed_completion(self) -> None:
        client = _BasicClient()
        adapter = BasicSafeHomeAdapter(client)

        result = await adapter.execute()

        self.assertEqual(result["status"], "SAFE_HOME_COMPLETED")
        self.assertTrue(result["physical_motion_completed"])
        self.assertEqual(
            result["integrated_controller_recovery"][
                "required_before_next_integrated_preview"
            ],
            "EXPLICIT_APPROVED_HOT",
        )
        self.assertEqual(client.safe_home_count, 1)

    async def test_failed_safe_home_is_not_reported_as_complete(self) -> None:
        client = _BasicClient()
        client.success = False
        adapter = BasicSafeHomeAdapter(client)

        result = await adapter.execute()

        self.assertEqual(result["status"], "SAFE_HOME_FAILED")
        self.assertFalse(result["physical_motion_completed"])

    async def test_disconnected_provider_must_be_activated_first(self) -> None:
        client = _BasicClient(state="DISCONNECTED")
        adapter = BasicSafeHomeAdapter(client)

        result = await adapter.execute()

        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(result["workflow_complete"])
        self.assertFalse(result["physical_motion_completed"])
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": "robot_arm.rebot_dm",
                    "action": "hot",
                    "required_capability": "robot_arm.safe_home",
                },
            },
        )
        self.assertEqual(client.safe_home_count, 0)


if __name__ == "__main__":
    unittest.main()
