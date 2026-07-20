from __future__ import annotations

import unittest

from physical_agent_test.initialize_space_cognition_skill import InitializeSpaceCognitionSkill


class _Manager:
    def __init__(self) -> None:
        self.calls = 0

    async def provider_request(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("500 after reset status publish")

    async def set_hot(self, *_args, **_kwargs):
        return {"status": "already_hot"}


class _Fabric:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.index = 0

    async def latest_optional(self, _stream):
        if not self.statuses:
            return None
        value = self.statuses[min(self.index, len(self.statuses) - 1)]
        self.index += 1
        return {"data": value}


class InitializeSkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_when_reset_epoch_changed_despite_control_500(self) -> None:
        old = {"session_epoch": "old", "tracking_state": "TRACKING"}
        new = {
            "session_epoch": "new",
            "world_frame": "local_vio/new",
            "tracking_state": "INITIALIZING",
        }
        manager = _Manager()
        fabric = _Fabric([old, new])
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=10.0,
        )

        result = await skill._request_vio_initialization(
            force_reset=False,
            related_skill_id="skill",
        )

        self.assertEqual(result["status"], "accepted_after_control_response_error")
        self.assertEqual(result["session_epoch"], "new")
        self.assertEqual(manager.calls, 1)

    async def test_waits_until_vio_observes_motion_inhibit(self) -> None:
        manager = _Manager()
        fabric = _Fabric([
            {"motion_inhibited": False},
            {"motion_inhibited": True},
        ])
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=10.0,
        )

        await skill._wait_for_vio_motion_inhibit()
        self.assertGreaterEqual(fabric.index, 2)


if __name__ == "__main__":
    unittest.main()
