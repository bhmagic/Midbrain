from __future__ import annotations

from unittest import mock
import unittest

from contact_work_runtime import ContactStep, ContactWorkRuntime, ProviderIdentity


class FakeClient:
    def __init__(
        self,
        fail_move=False,
        fail_release=False,
        fail_session=False,
        transition_time_s=0.0,
    ):
        self.fail_move = fail_move
        self.fail_release = fail_release
        self.fail_session = fail_session
        self.transition_time_s = float(transition_time_s)
        self.posts = []
        self.state = {
            "ready": True,
            "provider_id": "robot_arm.primary.contact",
            "provider_instance_id": "instance",
            "provider_boot_id": "boot",
            "assembly_fingerprint": "a" * 64,
            "mounted_effector_revision": "blade-v3",
            "acting_frame_id": "knife_tip",
            "root_frame_id": "arm_base",
            "arm_resource_id": "robot_arm.primary/arm",
        }

    def get(self, url):
        return dict(self.state)

    def post(self, url, payload, headers=None):
        self.posts.append((url, payload, headers or {}))
        if url.endswith("/v1/control-authority/leases"):
            return {
                "resource_id": payload["resource_id"],
                "lease_id": "manager-lease",
                "owner_id": payload["owner_id"],
                "fencing_generation": 7,
                "permissions": list(payload["permissions"]),
            }
        if url.endswith("/v1/contact/session"):
            if self.fail_session:
                raise TimeoutError("simulated ambiguous session response")
            return {"session_id": payload["plan"]["execution_id"]}
        if url.endswith("/v1/contact/move"):
            if self.fail_move:
                raise RuntimeError("simulated move failure")
            return {
                "disposition": "ACCEPTED",
                "velocity_limited_transition_time_s": self.transition_time_s,
            }
        if url.endswith("/v1/contact/relax"):
            return {"disposition": "EXPLICITLY_RELAXED", "float_confirmed": True}
        if url.endswith("/release"):
            if self.fail_release:
                raise RuntimeError("simulated Manager release failure")
            return {"state": "RELEASED"}
        if url.endswith("/renew"):
            return {"state": "ACTIVE"}
        raise AssertionError(f"unexpected POST {url}")


class SkillBuilderTests(unittest.TestCase):
    def test_velocity_limited_transition_extends_profile_delay(self):
        runtime = ContactWorkRuntime(
            client=FakeClient(),
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        self.assertAlmostEqual(
            runtime.effective_hold_s(
                0.25,
                {"velocity_limited_transition_time_s": 1.2},
            ),
            1.6,
        )
        self.assertEqual(
            runtime.effective_hold_s(
                2.0,
                {"velocity_limited_transition_time_s": 1.2},
            ),
            2.0,
        )

    def test_transition_time_does_not_consume_post_transition_watchdog(self):
        client = FakeClient(transition_time_s=0.02)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
            velocity_transition_margin_ratio=1.0,
            velocity_transition_margin_s=0.01,
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
            next_command_timeout_s=0.10,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            result = runtime.execute("contact.slicing", [step])
        self.assertEqual(result["submitted_step_count"], 1)
        self.assertTrue(any(item[0].endswith("/v1/contact/relax") for item in client.posts))

    def test_hold_beyond_transition_plus_watchdog_relaxes(self):
        client = FakeClient(transition_time_s=0.02)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
            next_command_timeout_s=0.10,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "transition-plus-watchdog window",
            ):
                runtime.execute("contact.slicing", [step])
        self.assertTrue(any(item[0].endswith("/v1/contact/relax") for item in client.posts))

    def test_runtime_signs_exact_plan_and_relaxes(self):
        client = FakeClient()
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            result = runtime.execute("contact.slicing", [step])
        self.assertEqual(result["submitted_step_count"], 1)
        self.assertEqual(len(result["move_tracking_observations"]), 1)
        session_call = next(item for item in client.posts if item[0].endswith("/v1/contact/session"))
        self.assertIn("X-Midbrain-Authorization", session_call[2])
        self.assertTrue(any(item[0].endswith("/v1/contact/relax") for item in client.posts))
        self.assertTrue(any(item[0].endswith("/release") for item in client.posts))

    def test_move_failure_still_requests_relax_and_releases_manager_authority(self):
        client = FakeClient(fail_move=True)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated move failure"):
                runtime.execute("contact.slicing", [step])
        self.assertTrue(any(item[0].endswith("/v1/contact/relax") for item in client.posts))
        self.assertTrue(any(item[0].endswith("/release") for item in client.posts))

    def test_manager_release_failure_does_not_mask_primary_move_failure(self):
        client = FakeClient(fail_move=True, fail_release=True)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated move failure"):
                runtime.execute("contact.slicing", [step])

    def test_manager_release_failure_is_reported_after_success(self):
        client = FakeClient(fail_release=True)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            result = runtime.execute("contact.slicing", [step])
        self.assertIn(
            "simulated Manager release failure",
            result["manager_authority_release_error"],
        )

    def test_ambiguous_session_response_still_requests_relax(self):
        client = FakeClient(fail_session=True)
        runtime = ContactWorkRuntime(
            client=client,
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        step = ContactStep(
            (0.2, 0.0, 0.3),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 4.0),
            delay_after_accept_s=0.0,
        )
        with mock.patch.dict(
            "os.environ",
            {"MIDBRAIN_CONTACT_SKILL_SECRET": "runtime-test-secret-with-at-least-32-bytes"},
            clear=False,
        ):
            with self.assertRaisesRegex(TimeoutError, "ambiguous session"):
                runtime.execute("contact.slicing", [step])
        self.assertTrue(any(item[0].endswith("/v1/contact/relax") for item in client.posts))


class PlanTests(unittest.TestCase):
    def test_plan_uses_provider_identity_and_contiguous_sequences(self):
        identity = ProviderIdentity(
            "provider",
            "instance",
            "boot",
            "a" * 64,
            "blade-v3",
            "knife_tip",
            "arm_base",
            "robot_arm.primary/arm",
        )
        runtime = ContactWorkRuntime(
            client=FakeClient(),
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        plan = runtime.build_plan(
            "contact.slicing",
            [
                ContactStep(
                    (0.2, 0.0, 0.3),
                    (0.0, 0.0, 0.0, 1.0),
                    (0.0, 0.0, 4.0),
                ),
                ContactStep(
                    (0.21, 0.0, 0.3),
                    (0.0, 0.0, 0.0, 1.0),
                    (0.0, 0.0, 4.0),
                ),
            ],
            identity=identity,
            manager_authority={
                "resource_id": "robot_arm.primary/arm",
                "lease_id": "manager-lease",
                "owner_id": "execution",
                "fencing_generation": 7,
                "permissions": ["execute_contact", "relax"],
            },
            execution_id="execution",
        )
        self.assertEqual([step["sequence"] for step in plan["steps"]], [0, 1])
        self.assertEqual(
            [step["motion_type"] for step in plan["steps"]],
            ["ONE_SHOT", "ONE_SHOT"],
        )
        self.assertEqual(
            [step["target"]["position_mode"] for step in plan["steps"]],
            ["ABSOLUTE_ROOT", "ABSOLUTE_ROOT"],
        )
        self.assertEqual(plan["assembly_fingerprint"], "a" * 64)

    def test_cartesian_segment_motion_type_is_signed_into_plan(self):
        identity = ProviderIdentity(
            "provider",
            "instance",
            "boot",
            "a" * 64,
            "blade-v3",
            "knife_tip",
            "arm_base",
            "robot_arm.primary/arm",
        )
        runtime = ContactWorkRuntime(
            client=FakeClient(),
            signing_secret_env="MIDBRAIN_CONTACT_SKILL_SECRET",
        )
        plan = runtime.build_plan(
            "contact.slicing",
            [
                ContactStep(
                    (0.2, 0.0, 0.3),
                    (0.0, 0.0, 0.0, 1.0),
                    (0.0, 0.0, 4.0),
                    motion_type="CARTESIAN_SEGMENT",
                    position_mode="RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
                )
            ],
            identity=identity,
            manager_authority={
                "resource_id": "robot_arm.primary/arm",
                "lease_id": "manager-lease",
                "owner_id": "execution",
                "fencing_generation": 7,
                "permissions": ["execute_contact", "relax"],
            },
            execution_id="execution",
        )
        self.assertEqual(plan["steps"][0]["motion_type"], "CARTESIAN_SEGMENT")
        self.assertEqual(
            plan["steps"][0]["target"]["position_mode"],
            "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
        )


if __name__ == "__main__":
    unittest.main()
