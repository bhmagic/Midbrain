from __future__ import annotations

import unittest

from physical_agent_test.initialize_space_cognition_skill import InitializeSpaceCognitionSkill


class _Manager:
    def __init__(self) -> None:
        self.calls = 0
        self.revocations = []

    async def provider_request(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("500 after reset status publish")

    async def set_hot(self, *_args, **_kwargs):
        return {"status": "already_hot"}

    async def workcell_calibrations(self):
        return {
            "activations": [
                {
                    "activation_id": "active-1",
                    "state": "ACTIVE",
                    "motion_usable": True,
                    "session_epoch": "epoch-old",
                },
                {
                    "activation_id": "expired-1",
                    "state": "EXPIRED",
                    "motion_usable": False,
                },
            ]
        }

    async def revoke_workcell_calibration(self, activation_id, **kwargs):
        self.revocations.append((activation_id, kwargs))
        return {"activation_id": activation_id, "state": "REVOKED"}


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

    async def test_forced_origin_reset_revokes_active_workcell_alignment(self) -> None:
        manager = _Manager()
        skill = InitializeSpaceCognitionSkill(
            manager,
            _Fabric([]),
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=10.0,
        )

        revoked = await skill._revoke_active_workcell_calibrations(
            skill_id="skill-1",
            previous_session_epoch="epoch-old",
        )

        self.assertEqual(revoked, ("active-1",))
        self.assertEqual(manager.revocations[0][0], "active-1")
        self.assertIn(
            "epoch-old",
            manager.revocations[0][1]["reason"],
        )

    async def test_post_reset_sweep_preserves_new_epoch_alignment(self) -> None:
        manager = _Manager()

        async def calibrations():
            return {
                "activations": [
                    {
                        "activation_id": "new-epoch",
                        "state": "ACTIVE",
                        "motion_usable": True,
                        "session_epoch": "epoch-new",
                    }
                ]
            }

        manager.workcell_calibrations = calibrations
        skill = InitializeSpaceCognitionSkill(
            manager,
            _Fabric([]),
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=10.0,
        )

        revoked = await skill._revoke_active_workcell_calibrations(
            skill_id="skill-1",
            previous_session_epoch="epoch-old",
            preserve_session_epoch="epoch-new",
        )

        self.assertEqual(revoked, ())
        self.assertEqual(manager.revocations, [])


if __name__ == "__main__":
    unittest.main()
