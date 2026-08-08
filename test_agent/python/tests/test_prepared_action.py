from __future__ import annotations

import unittest

from physical_agent_test.prepared_action import (
    CallScopedPreparedActionCoordinator,
)


class PreparedActionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.preview_count = 0
        self.executed: list[str] = []

        async def prepare(arguments):
            self.preview_count += 1
            preview_id = f"preview-{self.preview_count}"
            return {
                "status": "PREVIEW_READY",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "direction": arguments["direction"],
                "required_next_tool": {
                    "name": "execute_preview",
                    "arguments": {"preview_id": preview_id},
                },
            }

        def select(result):
            continuation = result.get("required_next_tool")
            if not isinstance(continuation, dict):
                return None
            if continuation.get("name") != "execute_preview":
                return None
            arguments = continuation.get("arguments")
            return arguments if isinstance(arguments, dict) else None

        async def resolve(arguments):
            return {
                "preview_id": arguments["preview_id"],
                "motion_intent": "NEW_RELATIVE_MOVE",
                "direction": "UP",
                "distance_m": 0.1,
            }

        async def execute(arguments):
            preview_id = arguments["preview_id"]
            self.executed.append(preview_id)
            return {
                "status": "MOTION_COMPLETED",
                "preview_id": preview_id,
            }

        self.coordinator = CallScopedPreparedActionCoordinator(
            prepare_action=prepare,
            select_continuation=select,
            resolve_authorization=resolve,
            execute_continuation=execute,
        )

    async def test_exact_call_executes_its_prepared_continuation(self) -> None:
        prepared = await self.coordinator.prepare_for_call(
            "call-1",
            {"direction": "UP"},
        )

        result = await self.coordinator.execute_for_call(
            "call-1",
            {"direction": "UP"},
        )

        self.assertTrue(prepared.executable)
        self.assertEqual(result["preview_id"], "preview-1")
        self.assertEqual(self.executed, ["preview-1"])

    async def test_identical_inputs_remain_scoped_to_distinct_calls(self) -> None:
        first = await self.coordinator.prepare_for_call(
            "call-1",
            {"direction": "UP"},
        )
        second = await self.coordinator.prepare_for_call(
            "call-2",
            {"direction": "UP"},
        )

        second_result = await self.coordinator.execute_for_call(
            "call-2",
            {"direction": "UP"},
        )
        first_result = await self.coordinator.execute_for_call(
            "call-1",
            {"direction": "UP"},
        )

        self.assertNotEqual(
            first.authorization_arguments["preview_id"],
            second.authorization_arguments["preview_id"],
        )
        self.assertEqual(second_result["preview_id"], "preview-2")
        self.assertEqual(first_result["preview_id"], "preview-1")

    async def test_changed_input_fails_closed_without_execution(self) -> None:
        await self.coordinator.prepare_for_call(
            "call-1",
            {"direction": "UP"},
        )

        result = await self.coordinator.execute_for_call(
            "call-1",
            {"direction": "DOWN"},
        )

        self.assertEqual(result["status"], "PREPARED_ACTION_INPUT_MISMATCH")
        self.assertFalse(result["physical_motion_submitted"])
        self.assertEqual(self.executed, [])

    async def test_missing_call_fails_closed_without_repreparing(self) -> None:
        result = await self.coordinator.execute_for_call(
            "unknown-call",
            {"direction": "UP"},
        )

        self.assertEqual(result["status"], "PREPARED_ACTION_STATE_UNAVAILABLE")
        self.assertIsNone(result["physical_motion_submitted"])
        self.assertFalse(result["new_continuation_submitted"])
        self.assertEqual(self.preview_count, 0)
        self.assertEqual(self.executed, [])

    async def test_nonmatching_continuation_returns_preparation_result(self) -> None:
        async def prepare_dependency(_arguments):
            return {
                "status": "DEPENDENCY_UNAVAILABLE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "required_next_tool": {
                    "name": "set_provider_residency",
                    "arguments": {"provider_id": "integrated"},
                },
            }

        coordinator = CallScopedPreparedActionCoordinator(
            prepare_action=prepare_dependency,
            select_continuation=lambda _result: None,
            resolve_authorization=lambda _arguments: None,
            execute_continuation=lambda _arguments: None,
        )
        prepared = await coordinator.prepare_for_call("call-1", {})

        result = await coordinator.execute_for_call("call-1", {})

        self.assertFalse(prepared.executable)
        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
