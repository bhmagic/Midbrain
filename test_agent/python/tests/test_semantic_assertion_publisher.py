from __future__ import annotations

import unittest

from physical_agent_test.semantic_assertion_publisher import (
    SemanticAssertionPublisher,
)


class FakeFabric:
    def __init__(self) -> None:
        self.observations = []

    async def publish(self, observation):
        self.observations.append(observation)
        return {"accepted": True}


def metric_item(object_id: str, center: list[float]) -> dict:
    return {
        "eligible_for_control_math": True,
        "object_id": object_id,
        "target_frame": "rebot_arm_base",
        "observed_at_us": 100,
        "contact_policy": "NO_CONTACT",
        "location": {
            "target_point_m": center,
            "uncertainty_radius_m": 0.003,
        },
        "volume_hint": {"raw_sphere_radius_m": 0.06},
        "skill_id": f"locate-{object_id}",
    }


class SemanticAssertionPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_metric_item_is_published_as_short_lived_workpiece(self) -> None:
        fabric = FakeFabric()
        publisher = SemanticAssertionPublisher(fabric)

        result = await publisher.publish_item_location(
            metric_item("toilet-paper", [0.3, 0.1, 0.2])
        )

        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["type"], "WORKPIECE")
        observation = fabric.observations[0]
        self.assertEqual(
            observation["schema"],
            "physical_agent.arm_semantic_assertions",
        )
        assertion = observation["data"]["assertions"][0]
        self.assertEqual(assertion["object_id"], "toilet-paper")
        self.assertEqual(assertion["type"], "WORKPIECE")
        self.assertEqual(assertion["contact_policy"], "NO_CONTACT")

    async def test_volume_centroid_is_distinct_from_surface_control_point(self):
        fabric = FakeFabric()
        publisher = SemanticAssertionPublisher(fabric)
        item = metric_item("toilet-paper", [0.42, 0.12, 0.17])
        item["volume_hint"] = {
            "method": "FRONT_SURFACE_PROJECTED_CROSS_SECTION_CENTROID_V1",
            "estimated_centroid_target_m": [0.36, 0.14, 0.13],
            "representative_sphere_radius_m": 0.069,
        }

        await publisher.publish_item_location(item)

        assertion = fabric.observations[0]["data"]["assertions"][0]
        self.assertEqual(assertion["center_m"], [0.36, 0.14, 0.13])
        self.assertEqual(assertion["radius_m"], 0.069)
        self.assertEqual(
            assertion["geometry_method"],
            "FRONT_SURFACE_PROJECTED_CROSS_SECTION_CENTROID_V1",
        )

    async def test_multiple_objects_are_merged_in_the_same_frame(self) -> None:
        fabric = FakeFabric()
        publisher = SemanticAssertionPublisher(fabric)
        await publisher.publish_item_location(
            metric_item("first", [0.3, 0.1, 0.2])
        )
        await publisher.publish_item_location(
            metric_item("second", [0.4, 0.1, 0.2])
        )

        assertions = fabric.observations[-1]["data"]["assertions"]
        self.assertEqual(
            {value["object_id"] for value in assertions},
            {"first", "second"},
        )

    async def test_nonmetric_item_is_not_published(self) -> None:
        fabric = FakeFabric()
        publisher = SemanticAssertionPublisher(fabric)
        result = await publisher.publish_item_location(
            {"eligible_for_control_math": False}
        )
        self.assertEqual(result["status"], "NOT_PUBLISHED_NONMETRIC")
        self.assertEqual(fabric.observations, [])
