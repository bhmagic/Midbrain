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
            self.value.updated_at_us = time.time_ns() // 1000
            snapshot = self.value.snapshot()
        await self._publish(snapshot)
        return snapshot

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return self.value.snapshot()

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
                    "stream": "skills.vegetable_cutting.status",
                    "provider_id": "skill.vegetable_cutting",
                    "provider_instance_id": payload["skill_id"],
                    "boot_id": payload["skill_id"],
                    "sequence": self.sequence,
                    "observed_at_us": now_us,
                    "freshness_ms": None,
                    "related_skill_id": payload["skill_id"],
                    "valid": payload.get("state") not in {"FAILED", "ABORTED"},
                    "data": payload,
                }
            )
        except Exception:
            pass
