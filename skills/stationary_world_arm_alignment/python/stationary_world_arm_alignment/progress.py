from __future__ import annotations

import asyncio
import time
from typing import Any

from .clients import FabricClient
from .models import Progress


class ProgressReporter:
    def __init__(self, fabric: FabricClient):
        self.fabric = fabric
        self.value = Progress()
        self.lock = asyncio.Lock()
        self.sequence = 0

    async def update(self, **changes: Any) -> dict[str, Any]:
        async with self.lock:
            for key, value in changes.items():
                if not hasattr(self.value, key):
                    raise AttributeError(f"unknown progress field: {key}")
                setattr(self.value, key, value)
            now_us = time.time_ns() // 1000
            self.value.updated_at_us = now_us
            if self.value.started_at_us:
                self.value.elapsed_s = max(
                    0.0,
                    (now_us - self.value.started_at_us) / 1_000_000.0,
                )
            snapshot = self.value.snapshot()
        await self._publish(snapshot)
        return snapshot

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            snapshot = self.value.snapshot()
            if self.value.started_at_us and str(self.value.state) == "RUNNING":
                snapshot["elapsed_s"] = max(
                    0.0,
                    (time.time_ns() // 1000 - self.value.started_at_us) / 1_000_000.0,
                )
            return snapshot

    async def _publish(self, payload: dict[str, Any]) -> None:
        if not payload.get("skill_id"):
            return
        self.sequence += 1
        now_us = time.time_ns() // 1000
        try:
            await self.fabric.publish(
                {
                    "schema": "physical_agent.skill_status",
                    "schema_version": 1,
                    "stream": "skills.stationary_world_arm_alignment.status",
                    "provider_id": "skill.stationary_world_arm_alignment",
                    "provider_instance_id": payload["skill_id"],
                    "boot_id": payload["skill_id"],
                    "sequence": self.sequence,
                    "observed_at_us": now_us,
                    "freshness_ms": None,
                    "related_skill_id": payload["skill_id"],
                    "valid": payload.get("state") not in {"FAILED", "CANCELLED"},
                    "data": payload,
                }
            )
        except Exception:
            # The local GUI remains useful while Fabric is temporarily unavailable.
            pass
