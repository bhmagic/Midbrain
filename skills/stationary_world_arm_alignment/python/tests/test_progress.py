from __future__ import annotations

import asyncio
import time
import unittest

from stationary_world_arm_alignment.models import SkillState
from stationary_world_arm_alignment.progress import ProgressReporter


class FakeFabric:
    async def publish(self, _: dict) -> dict:
        return {"accepted": True}


class ProgressReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_elapsed_time_does_not_keep_increasing(self) -> None:
        reporter = ProgressReporter(FakeFabric())
        await reporter.update(
            skill_id="skill-test",
            state=SkillState.RUNNING,
            started_at_us=time.time_ns() // 1000 - 1_000_000,
        )
        await reporter.update(state=SkillState.SUCCEEDED)
        terminal = await reporter.snapshot()
        await asyncio.sleep(0.02)
        later = await reporter.snapshot()
        self.assertEqual(terminal["elapsed_s"], later["elapsed_s"])
