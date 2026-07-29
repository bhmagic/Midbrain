from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import pytest

pytest.importorskip("agents")

from physical_agent_test.agent_driver import PrototypeAgentDriver
from physical_agent_test.stationary_calibration_adapter import (
    StationaryCalibrationSkillAdapter,
)


class _Runtime:
    def __init__(self):
        self.calls = []
        self.cancelled = False
        self.closed = False

    async def run(
        self,
        mode,
        *,
        arm_is_home=False,
        allow_active_control_interrupt=False,
    ):
        self.calls.append(
            (mode, arm_is_home, allow_active_control_interrupt)
        )
        return {"status": "CANDIDATE_REVIEW_REQUIRED"}

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True


class _PointingSkill:
    async def run(self, question: str) -> str:
        return question


class StationaryCalibrationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_is_deferred_and_does_not_claim_home_or_interrupt(
        self,
    ) -> None:
        created = []

        def factory():
            runtime = _Runtime()
            created.append(runtime)
            return runtime

        adapter = StationaryCalibrationSkillAdapter(factory)
        self.assertEqual(created, [])

        result = await adapter.run(request="calibrate the stationary workcell")

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].calls, [("auto", False, False)])
        self.assertFalse(
            result["agent_adapter"]["physical_motion_submitted_by_adapter"]
        )
        self.assertFalse(result["agent_adapter"]["arm_is_home_claimed"])
        self.assertFalse(
            result["agent_adapter"]["active_control_interrupt_allowed"]
        )
        self.assertTrue(created[0].closed)

    async def test_manifest_tool_requires_approval(self) -> None:
        adapter = StationaryCalibrationSkillAdapter(_Runtime)
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=Path(__file__).resolve().parents[3],
            eligible_tool_names={"calibrate_stationary_workcell"},
            stationary_calibration_skill=adapter,
        )
        tool = driver.agent.tools[0]

        self.assertEqual(tool.name, "calibrate_stationary_workcell")
        self.assertTrue(tool.needs_approval)

    async def test_concurrent_run_is_rejected(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class _BlockingRuntime(_Runtime):
            async def run(self, *_args, **_kwargs):
                entered.set()
                await release.wait()
                return {"status": "DONE"}

        adapter = StationaryCalibrationSkillAdapter(_BlockingRuntime)
        first = asyncio.create_task(adapter.run(request="first"))
        await entered.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                await adapter.run(request="second")
        finally:
            release.set()
            await first


if __name__ == "__main__":
    unittest.main()
