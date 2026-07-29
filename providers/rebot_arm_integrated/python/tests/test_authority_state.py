from __future__ import annotations

import unittest

from rebot_arm_integrated.authority_state import (
    evaluate_authority_coordination,
)


RESOURCE_ID = "robot_arm.primary"


def manager_view(active_lease=None):
    return {
        "resource_id": RESOURCE_ID,
        "enforcement": "ADVISORY",
        "active_lease": active_lease,
        "latest_fencing_generation": (
            0 if active_lease is None else active_lease["fencing_generation"]
        ),
    }


def manager_lease():
    return {
        "lease_id": "manager-lease-1",
        "owner_id": "skill-1",
        "fencing_generation": 3,
        "state": "ACTIVE",
    }


def local_lease():
    return {
        "active": True,
        "lease_id": "basic-lease-1",
        "fencing_generation": 19,
        "state": "OWNED",
    }


class AuthorityStateTests(unittest.TestCase):
    def evaluate(self, **overrides):
        arguments = {
            "resource_id": RESOURCE_ID,
            "manager_available": True,
            "manager_view": manager_view(),
            "local_basic_lease": None,
            "integrated_residency": "WARM",
            "integrated_control_state": "IDLE",
            "observed_at_us": 123,
        }
        arguments.update(overrides)
        return evaluate_authority_coordination(**arguments)

    def test_idle_state_is_consistent(self):
        result = self.evaluate()
        self.assertEqual(result["state"], "NO_ACTIVE_AUTHORITY")
        self.assertTrue(result["consistent"])
        self.assertEqual(result["disagreement_reasons"], [])

    def test_dual_layer_authority_requires_explicit_lineage(self):
        result = self.evaluate(
            manager_view=manager_view(manager_lease()),
            local_basic_lease=local_lease(),
            local_writer_active=True,
            integrated_residency="HOT",
        )
        self.assertEqual(result["state"], "DUAL_LAYER_UNCORRELATED")
        self.assertIn(
            "AUTHORITY_LINEAGE_NOT_BOUND",
            result["disagreement_reasons"],
        )
        self.assertNotEqual(
            result["fencing"]["manager_generation"],
            result["fencing"]["local_basic_generation"],
        )
        self.assertFalse(
            result["fencing"]["numeric_equality_has_meaning"],
        )

    def test_matching_upstream_lineage_is_coordinated(self):
        result = self.evaluate(
            manager_view=manager_view(manager_lease()),
            local_basic_lease=local_lease(),
            local_writer_active=True,
            integrated_residency="HOT",
            upstream_owner_id="skill-1",
            upstream_authority_lease_id="manager-lease-1",
        )
        self.assertEqual(result["state"], "COORDINATED_ACTIVE")
        self.assertTrue(result["consistent"])
        self.assertTrue(result["lineage"]["bound"])

    def test_local_only_and_manager_only_are_distinct(self):
        local = self.evaluate(
            local_basic_lease=local_lease(),
            local_writer_active=True,
        )
        self.assertEqual(local["state"], "PROVIDER_LOCAL_ONLY")
        self.assertIn(
            "MANAGER_AUTHORITY_ABSENT_FOR_LOCAL_WRITER",
            local["disagreement_reasons"],
        )

        manager = self.evaluate(
            manager_view=manager_view(manager_lease()),
        )
        self.assertEqual(manager["state"], "MANAGER_ONLY")
        self.assertTrue(manager["consistent"])

    def test_idle_local_lease_is_standby_not_a_writer_conflict(self):
        local = self.evaluate(local_basic_lease=local_lease())
        self.assertEqual(local["state"], "LOCAL_LEASE_STANDBY")
        self.assertTrue(local["consistent"])
        self.assertFalse(local["local_writer_active"])

        manager = self.evaluate(
            manager_view=manager_view(manager_lease()),
            local_basic_lease=local_lease(),
        )
        self.assertEqual(
            manager["state"],
            "MANAGER_PRESENT_LOCAL_STANDBY",
        )
        self.assertTrue(manager["consistent"])
        self.assertNotIn(
            "AUTHORITY_LINEAGE_NOT_BOUND",
            manager["disagreement_reasons"],
        )

    def test_manager_loss_keeps_local_fencing_visible_without_enforcement(self):
        result = self.evaluate(
            manager_available=False,
            manager_view=None,
            local_basic_lease=local_lease(),
            local_writer_active=True,
        )
        self.assertEqual(
            result["state"],
            "MANAGER_UNAVAILABLE_WITH_LOCAL_LEASE",
        )
        self.assertIn("MANAGER_UNAVAILABLE", result["disagreement_reasons"])
        self.assertEqual(
            result["local_basic_lease"]["lease_id"],
            "basic-lease-1",
        )
        self.assertFalse(result["effect"]["physical_enforcement"])

    def test_inhibit_and_relinquishment_conflicts_are_explicit(self):
        result = self.evaluate(
            local_basic_lease=local_lease(),
            local_writer_active=True,
            motion_inhibited=True,
            relinquishment_state="STARTING",
        )
        self.assertIn(
            "MOTION_INHIBIT_WITH_LOCAL_WRITER",
            result["disagreement_reasons"],
        )
        self.assertIn(
            "RELINQUISHMENT_PENDING_WITH_LOCAL_WRITER",
            result["disagreement_reasons"],
        )

    def test_incomplete_lease_identities_fail_closed_in_evidence(self):
        result = self.evaluate(
            manager_view=manager_view(
                {
                    "owner_id": "skill-1",
                    "fencing_generation": 3,
                }
            ),
            local_basic_lease={"active": True},
            local_writer_active=True,
        )
        self.assertIn(
            "MANAGER_LEASE_IDENTITY_INCOMPLETE",
            result["disagreement_reasons"],
        )
        self.assertIn(
            "LOCAL_BASIC_LEASE_IDENTITY_INCOMPLETE",
            result["disagreement_reasons"],
        )

    def test_writer_without_basic_lease_is_explicit(self):
        result = self.evaluate(local_writer_active=True)
        self.assertEqual(result["state"], "PROVIDER_LOCAL_ONLY")
        self.assertIn(
            "LOCAL_WRITER_WITHOUT_BASIC_LEASE",
            result["disagreement_reasons"],
        )
        self.assertIn(
            "MANAGER_AUTHORITY_ABSENT_FOR_LOCAL_WRITER",
            result["disagreement_reasons"],
        )

    def test_equal_generation_numbers_do_not_establish_lineage(self):
        manager = manager_lease()
        local = local_lease()
        local["fencing_generation"] = manager["fencing_generation"]
        result = self.evaluate(
            manager_view=manager_view(manager),
            local_basic_lease=local,
            local_writer_active=True,
        )
        self.assertEqual(result["state"], "DUAL_LAYER_UNCORRELATED")
        self.assertFalse(
            result["fencing"]["numeric_equality_has_meaning"],
        )


if __name__ == "__main__":
    unittest.main()
