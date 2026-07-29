from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
import unittest

from physical_agent_test.temporal_policy import (
    evaluate_observation_temporal_policy,
)


class TemporalPolicyTests(unittest.TestCase):
    def test_skill_policy_uses_source_time_not_fabric_receipt_age(self) -> None:
        now_us = time.time_ns() // 1000
        observation = {
            "observed_at_us": now_us - 50_000,
            "received_at": (
                datetime.now(timezone.utc) - timedelta(seconds=5)
            ).isoformat(),
            "freshness_ms": 10,
            "valid": True,
        }

        evidence = evaluate_observation_temporal_policy(
            observation_name="rgb",
            observation=observation,
            policy_id="skill.test.v1",
            maximum_source_age_ms=100,
            now_us=now_us,
        )

        self.assertEqual(evidence.decision, "ACCEPT")
        self.assertEqual(evidence.source_age_ms, 50.0)
        self.assertEqual(evidence.producer_recommended_max_age_ms, 10.0)
        self.assertGreater(evidence.receipt_age_ms or 0.0, 4000.0)

    def test_skill_policy_rejects_old_source_even_if_just_received(self) -> None:
        now_us = time.time_ns() // 1000
        observation = {
            "observed_at_us": now_us - 2_000_000,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "freshness_ms": 10_000,
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "exceeds Skill temporal policy",
        ):
            evaluate_observation_temporal_policy(
                observation_name="depth",
                observation=observation,
                policy_id="skill.test.v1",
                maximum_source_age_ms=500,
                now_us=now_us,
            )

    def test_producer_hard_expiry_is_not_consumer_dependent(self) -> None:
        now_us = time.time_ns() // 1000
        with self.assertRaisesRegex(RuntimeError, "hard expiry"):
            evaluate_observation_temporal_policy(
                observation_name="buffer",
                observation={
                    "observed_at_us": now_us - 10_000,
                    "expires_at_us": now_us - 1,
                },
                policy_id="skill.test.v1",
                maximum_source_age_ms=None,
                now_us=now_us,
            )

    def test_missing_source_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no source timestamp"):
            evaluate_observation_temporal_policy(
                observation_name="pose",
                observation={},
                policy_id="skill.test.v1",
                maximum_source_age_ms=100,
            )
