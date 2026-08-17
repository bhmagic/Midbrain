from __future__ import annotations

import unittest
import time
from pathlib import Path

from jsonschema import validate

from physical_agent_test.initialize_space_cognition_skill import InitializeSpaceCognitionSkill
from physical_agent_test.skill_catalog import discover_agent_skills
from physical_agent_test.spatial_frames import WORLD_CONVENTION_ID


WORKSPACE = Path(__file__).resolve().parents[3]


def _validate_establish_world_axis_result(result) -> None:
    descriptor = next(
        item
        for item in discover_agent_skills(WORKSPACE, include_disabled=True)
        if item.tool_name == "establish_world_axis"
    )
    validate(instance=result, schema=descriptor.output_schema)


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


class _VerificationManager:
    def __init__(self, fabric=None):
        self.fabric = fabric
        self.hot: list[str] = []
        self.provider_request_calls = 0
        self.inhibit_acquire_calls = 0
        self.inhibit_release_calls = 0
        self.provider_requests: list[dict] = []

    async def acquire_motion_inhibit(self, **_kwargs):
        self.inhibit_acquire_calls += 1
        if self.fabric is not None:
            self.fabric.motion_inhibited = True
            self.fabric.tracking_state = "TRACKING"
        return {"active": True, "lease_id": "inhibit-1"}

    async def release_motion_inhibit(self, **_kwargs):
        self.inhibit_release_calls += 1
        return {"active": False}

    async def set_hot(self, provider_id):
        self.hot.append(provider_id)
        return {"status": "hot"}

    async def provider_request(self, provider_id, **kwargs):
        self.provider_request_calls += 1
        self.provider_requests.append(
            {"provider_id": provider_id, **kwargs}
        )
        if self.fabric is not None:
            self.fabric.tracking_state = "TRACKING"
        return {
            "status": "fixed_rig_stationary_attested",
            "epoch_reset": False,
        }


class _VerificationFabric:
    def __init__(self, *, tracking_state="TRACKING"):
        self.published: list[dict] = []
        self.tracking_state = tracking_state
        self.motion_inhibited = True

    async def latest_optional(self, stream):
        now_us = time.time_ns() // 1000
        common = {
            "valid": True,
            "observed_at_us": now_us,
            "freshness_ms": 1000,
        }
        if stream == "localization.vio.status":
            return {
                **common,
                "data": {
                    "motion_inhibited": self.motion_inhibited,
                    "tracking_state": self.tracking_state,
                    "session_epoch": "epoch-current",
                    "world_frame": "local_vio/epoch-current",
                    "convention_id": WORLD_CONVENTION_ID,
                },
            }
        if stream == "localization.body.pose":
            return {
                **common,
                "freshness_ms": 500,
                "data": {
                    "body_frame": "body_base",
                    "position_m": [0.0, 0.0, 0.0],
                    "session_epoch": "epoch-current",
                    "world_frame": "local_vio/epoch-current",
                    "convention_id": WORLD_CONVENTION_ID,
                },
            }
        return {**common, "data": {}}

    async def publish(self, observation):
        self.published.append(observation)


class InitializeSkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_tracking_uses_motion_inhibit_without_reset(self) -> None:
        fabric = _VerificationFabric(tracking_state="INITIALIZING")
        manager = _VerificationManager(fabric)
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=1.0,
        )

        result = await skill.ensure_tracking()

        _validate_establish_world_axis_result(result)
        self.assertEqual(result["status"], "tracking_ready")
        self.assertEqual(
            result["result"]["stationary_gate"],
            "GLOBAL_MOTION_INHIBIT",
        )
        self.assertFalse(result["result"]["epoch_reset_performed"])
        self.assertEqual(manager.hot, ["camera", "vio"])
        self.assertEqual(manager.inhibit_acquire_calls, 1)
        self.assertEqual(manager.inhibit_release_calls, 1)
        self.assertEqual(manager.provider_request_calls, 0)

    async def test_ensure_tracking_reuses_existing_epoch_contract(self) -> None:
        fabric = _VerificationFabric(tracking_state="TRACKING")
        manager = _VerificationManager(fabric)
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=1.0,
        )

        result = await skill.ensure_tracking()

        _validate_establish_world_axis_result(result)
        self.assertEqual(result["status"], "tracking_ready")
        self.assertEqual(
            result["result"]["stationary_gate"],
            "EXISTING_TRACKING_EPOCH",
        )
        self.assertEqual(manager.inhibit_acquire_calls, 0)
        self.assertEqual(manager.inhibit_release_calls, 0)

    async def test_fixed_rig_tracking_check_does_not_reset_epoch(self) -> None:
        fabric = _VerificationFabric()
        manager = _VerificationManager(fabric)
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=1.0,
        )

        result = await skill.verify_tracking(
            fixed_rig_confirmed=True
        )

        self.assertEqual(result["status"], "tracking_ready")
        self.assertEqual(
            result["result"]["session_epoch"],
            "epoch-current",
        )
        self.assertFalse(result["result"]["epoch_reset_performed"])
        self.assertEqual(manager.provider_request_calls, 0)
        self.assertEqual(manager.hot, ["camera", "vio"])
        self.assertEqual(manager.inhibit_acquire_calls, 0)
        self.assertEqual(manager.inhibit_release_calls, 0)
        self.assertEqual(
            fabric.published[-1]["data"]["state"],
            "SUCCEEDED",
        )

    async def test_initializing_fixed_rig_uses_vio_attestation_not_inhibit(
        self,
    ) -> None:
        fabric = _VerificationFabric(tracking_state="INITIALIZING")
        manager = _VerificationManager(fabric)
        skill = InitializeSpaceCognitionSkill(
            manager,
            fabric,
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=1.0,
        )

        result = await skill.verify_tracking(
            fixed_rig_confirmed=True
        )

        self.assertEqual(result["status"], "tracking_ready")
        self.assertEqual(manager.provider_request_calls, 1)
        request = manager.provider_requests[0]
        self.assertEqual(
            request["action"],
            "attest_fixed_rig_stationary",
        )
        self.assertTrue(
            request["payload"]["fixed_rig_confirmed"]
        )
        self.assertEqual(manager.inhibit_acquire_calls, 0)
        self.assertEqual(manager.inhibit_release_calls, 0)
        self.assertFalse(
            result["result"]["global_motion_inhibit_acquired"]
        )
        self.assertEqual(
            result["result"]["stationary_gate"],
            "FIXED_RIG_OPERATOR_ATTESTATION",
        )

    async def test_fixed_rig_confirmation_is_required(self) -> None:
        skill = InitializeSpaceCognitionSkill(
            _VerificationManager(),
            _VerificationFabric(),
            camera_provider_id="camera",
            vio_provider_id="vio",
            timeout_s=1.0,
        )

        with self.assertRaisesRegex(ValueError, "confirmation"):
            await skill.verify_tracking(fixed_rig_confirmed=False)

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
