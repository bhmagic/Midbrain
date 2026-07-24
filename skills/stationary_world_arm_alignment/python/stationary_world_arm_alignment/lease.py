from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .clients import ManagerClient


class MotionInhibitKeeper:
    """Use renewable leases when supported and remain compatible with the current Manager."""

    def __init__(
        self,
        manager: ManagerClient,
        *,
        owner_id: str,
        related_skill_id: str,
        duration_ms: int,
        renew_every_ms: int,
        failure_limit: int,
    ):
        self.manager = manager
        self.owner_id = owner_id
        self.related_skill_id = related_skill_id
        self.duration_ms = duration_ms
        self.renew_every_s = renew_every_ms / 1000.0
        self.failure_limit = failure_limit
        self.lease_id: str | None = None
        self.mode = "not_acquired"
        self.failures = 0
        self.fatal_error: str | None = None
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def acquire(self) -> dict[str, Any]:
        response = await self.manager.acquire_motion_inhibit(
            owner_id=self.owner_id,
            reason="stationary world-space arm alignment",
            related_skill_id=self.related_skill_id,
            duration_ms=self.duration_ms,
        )
        lease = response.get("lease") if isinstance(response.get("lease"), dict) else response
        self.lease_id = str(lease.get("lease_id")) if lease.get("lease_id") else None
        if self.lease_id:
            self.mode = "renewable_expiring"
            self.task = asyncio.create_task(self._renew_loop(), name="motion-inhibit-renewal")
        else:
            self.mode = "legacy_nonexpiring"
        return response

    async def _renew_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.renew_every_s)
                break
            except TimeoutError:
                pass
            try:
                await self.manager.renew_motion_inhibit(
                    owner_id=self.owner_id,
                    lease_id=str(self.lease_id),
                    duration_ms=self.duration_ms,
                )
                self.failures = 0
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    self.mode = "manager_renew_endpoint_missing"
                    return
                self.failures += 1
                if self.failures >= self.failure_limit:
                    self.fatal_error = f"motion-inhibit renewal failed: {error}"
                    return
            except Exception as error:
                self.failures += 1
                if self.failures >= self.failure_limit:
                    self.fatal_error = f"motion-inhibit renewal failed: {error}"
                    return

    async def ensure_valid(self) -> None:
        if self.fatal_error:
            raise RuntimeError(self.fatal_error)

    async def release(self) -> None:
        self.stop_event.set()
        if self.task:
            await self.task
            self.task = None
        await self.manager.release_motion_inhibit(owner_id=self.owner_id, lease_id=self.lease_id)
        self.mode = "released"

    def status(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "lease_id": self.lease_id,
            "mode": self.mode,
            "renewal_failures": self.failures,
            "fatal_error": self.fatal_error,
        }
