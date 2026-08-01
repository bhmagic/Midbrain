from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from physical_agent_test.phase4_policy import (
    OperationRegistry,
    Phase4Policy,
    extend_current_operation_hard_timeout,
    install_operation_registry,
    report_operation_progress,
)


class Phase4PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_keep_every_new_boundary_nonphysical(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            policy = Phase4Policy.from_environment()
        self.assertEqual(policy.binding, "SHADOW")
        self.assertEqual(policy.controller_audit, "SHADOW")
        self.assertEqual(policy.manager_authority, "SHADOW")
        self.assertEqual(policy.generic_rgbd_route, "SHADOW")
        self.assertEqual(policy.physical_execution, "DISABLED")
        self.assertFalse(policy.as_dict()["physical_authorization_inherited"])

    def test_invalid_or_coupled_timeout_configuration_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PHASE4_OPERATION_HARD_TIMEOUT_S": "1",
                "PHASE4_OPERATION_IDLE_TIMEOUT_S": "2",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must not exceed"):
                Phase4Policy.from_environment()

    async def test_idle_operation_is_cancelled_without_user_monitoring(self) -> None:
        registry = OperationRegistry()
        install_operation_registry(registry)

        async def stuck() -> None:
            await asyncio.sleep(5)

        with self.assertRaisesRegex(TimeoutError, "reported no progress"):
            await registry.run(
                "stuck-test",
                stuck(),
                hard_timeout_s=1.0,
                idle_timeout_s=0.05,
            )
        snapshot = registry.snapshot()
        self.assertEqual(snapshot["active_count"], 0)
        self.assertEqual(snapshot["operations"][0]["stage"], "IDLE_TIMEOUT")

    async def test_progress_heartbeat_prevents_idle_cancellation(self) -> None:
        registry = OperationRegistry()
        install_operation_registry(registry)

        async def progressing() -> str:
            for index in range(4):
                report_operation_progress(f"STEP_{index}")
                await asyncio.sleep(0.02)
            return "done"

        result = await registry.run(
            "progress-test",
            progressing(),
            hard_timeout_s=1.0,
            idle_timeout_s=0.05,
        )
        self.assertEqual(result, "done")
        self.assertEqual(registry.snapshot()["operations"][0]["state"], "SUCCEEDED")

    async def test_finite_operation_can_latch_a_longer_specific_deadline(
        self,
    ) -> None:
        registry = OperationRegistry()
        install_operation_registry(registry)

        async def calibration() -> str:
            extend_current_operation_hard_timeout(
                0.20,
                stage="FOUNDATION_POSE_CALIBRATION",
            )
            for index in range(3):
                await asyncio.sleep(0.03)
                report_operation_progress(
                    f"FOUNDATION_POSE_PROGRESS_{index}"
                )
            return "done"

        result = await registry.run(
            "calibration-test",
            calibration(),
            hard_timeout_s=0.05,
            idle_timeout_s=0.05,
        )

        self.assertEqual(result, "done")
        operation = registry.snapshot()["operations"][0]
        self.assertEqual(operation["state"], "SUCCEEDED")
        self.assertEqual(operation["hard_timeout_s"], 0.20)


if __name__ == "__main__":
    unittest.main()
