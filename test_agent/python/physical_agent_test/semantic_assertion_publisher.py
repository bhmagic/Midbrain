from __future__ import annotations

import math
import time
import uuid
from typing import Any, Protocol


SEMANTIC_ASSERTION_SCHEMA = "physical_agent.arm_semantic_assertions"
SEMANTIC_ASSERTION_STREAM = "robot_arm.scene.semantic_assertions"


class FabricPublishProtocol(Protocol):
    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]: ...


class SemanticAssertionPublisher:
    """Publish short-lived metric item assertions for the HOT scene compiler."""

    def __init__(
        self,
        fabric: FabricPublishProtocol,
        *,
        provider_id: str = "test_agent.metric_item_locator",
        stream: str = SEMANTIC_ASSERTION_STREAM,
        freshness_ms: int = 5000,
    ) -> None:
        if not 1 <= int(freshness_ms) <= 60_000:
            raise ValueError("semantic assertion freshness_ms is invalid")
        self.fabric = fabric
        self.provider_id = provider_id
        self.stream = stream
        self.freshness_ms = int(freshness_ms)
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.sequence = 0
        self.frame_id: str | None = None
        self.assertions: dict[str, dict[str, Any]] = {}

    async def publish_item_location(
        self,
        item_location: dict[str, Any],
    ) -> dict[str, Any]:
        if item_location.get("eligible_for_control_math") is not True:
            return {"status": "NOT_PUBLISHED_NONMETRIC"}
        object_id = str(item_location.get("object_id") or "").strip()
        frame_id = str(item_location.get("target_frame") or "").strip()
        location = item_location.get("location")
        location = location if isinstance(location, dict) else {}
        center = location.get("target_point_m")
        if not object_id or not frame_id or not isinstance(center, list) or len(center) != 3:
            raise ValueError("metric item location is incomplete for semantic publication")
        uncertainty = float(location.get("uncertainty_radius_m") or 0.0)
        volume = item_location.get("volume_hint")
        volume = volume if isinstance(volume, dict) else {}
        centroid = volume.get("estimated_centroid_target_m")
        if isinstance(centroid, list) and len(centroid) == 3:
            center = centroid
        projected_radius = float(
            volume.get("representative_sphere_radius_m")
            or volume.get("raw_sphere_radius_m")
            or 0.0
        )
        radius_m = max(uncertainty, projected_radius)
        if not math.isfinite(radius_m) or radius_m <= 0.0:
            raise ValueError("metric item location has no positive radius")

        now_us = time.time_ns() // 1000
        expires_at_us = now_us + self.freshness_ms * 1000
        if self.frame_id != frame_id:
            self.frame_id = frame_id
            self.assertions.clear()
        self.assertions = {
            key: value
            for key, value in self.assertions.items()
            if int(value.get("expires_at_us") or 0) >= now_us
        }
        self.assertions[object_id] = {
            "assertion_id": f"metric-item:{object_id}",
            "object_id": object_id,
            "center_m": [float(value) for value in center],
            "radius_m": radius_m,
            "geometry_method": str(volume.get("method") or "SURFACE_POINT"),
            "type": "WORKPIECE",
            "semantic_source": "METRIC_ITEM_LOCATOR",
            "contact_policy": str(
                item_location.get("contact_policy") or "WORKPIECE_CONTACT_ALLOWED"
            ),
            "source_observed_at_us": int(item_location.get("observed_at_us") or 0),
            "expires_at_us": expires_at_us,
        }
        self.sequence += 1
        observation = {
            "schema": SEMANTIC_ASSERTION_SCHEMA,
            "schema_version": 1,
            "stream": self.stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": now_us,
            "freshness_ms": self.freshness_ms,
            "expires_at_us": expires_at_us,
            "coordinate_frame": frame_id,
            "clock_domain": "system",
            "related_skill_id": item_location.get("skill_id"),
            "valid": True,
            "data": {
                "contract_version": 1,
                "frame_id": frame_id,
                "assertions": list(self.assertions.values()),
            },
        }
        accepted = await self.fabric.publish(observation)
        return {
            "status": "PUBLISHED",
            "stream": self.stream,
            "provider_id": self.provider_id,
            "sequence": self.sequence,
            "expires_at_us": expires_at_us,
            "object_id": object_id,
            "type": "WORKPIECE",
            "radius_m": radius_m,
            "fabric_result": accepted,
        }
