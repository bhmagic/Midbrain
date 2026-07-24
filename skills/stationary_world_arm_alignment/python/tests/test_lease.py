from __future__ import annotations

import unittest

from stationary_world_arm_alignment.lease import MotionInhibitKeeper


class FakeManager:
    def __init__(self, renewable: bool):
        self.renewable = renewable
        self.released = False

    async def acquire_motion_inhibit(self, **_: object) -> dict:
        return {"lease_id": "lease-1"} if self.renewable else {"inhibited": True}

    async def renew_motion_inhibit(self, **_: object) -> dict:
        return {"renewed": True}

    async def release_motion_inhibit(self, **_: object) -> dict:
        self.released = True
        return {"released": True}


class MotionInhibitKeeperTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_manager_is_detected_as_legacy(self) -> None:
        manager = FakeManager(renewable=False)
        keeper = MotionInhibitKeeper(
            manager,
            owner_id="owner",
            related_skill_id="skill",
            duration_ms=100,
            renew_every_ms=50,
            failure_limit=2,
        )
        await keeper.acquire()
        self.assertEqual(keeper.mode, "legacy_nonexpiring")
        await keeper.release()
        self.assertTrue(manager.released)
