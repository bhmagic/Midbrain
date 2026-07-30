from __future__ import annotations

import asyncio
from typing import Any

import httpx


class IntegratedControllerClient:
    def __init__(self, base_url: str, *, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def preview_transit_path(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/path-plan",
            json=request,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Integrated path preview returned a non-object result")
        return result

    async def state(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/state")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Integrated state returned a non-object result")
        return result

    async def preview_direct_motion(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/plan",
            json=request,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(
                "Integrated direct preview returned a non-object result"
            )
        return result

    async def engage_staged_motion(self) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/engage",
            json={"enabled": True},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Integrated engage returned a non-object result")
        return result

    async def trigger_one_shot_motion(self) -> dict[str, Any]:
        pressed_response = await self._client.post(
            f"{self.base_url}/v1/teleop",
            json={"lb": True},
        )
        pressed_response.raise_for_status()
        pressed = pressed_response.json()
        try:
            await asyncio.sleep(0.05)
        finally:
            released_response = await self._client.post(
                f"{self.base_url}/v1/teleop",
                json={"lb": False},
            )
            released_response.raise_for_status()
        if not isinstance(pressed, dict):
            raise RuntimeError(
                "Integrated one-shot trigger returned a non-object result"
            )
        return pressed

    async def commit_transit_path(
        self,
        request: dict[str, Any],
        *,
        authorization_assertion: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/path-commit",
            json=request,
            headers={
                "X-Midbrain-Authorization": authorization_assertion,
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(
                "Integrated path commit returned a non-object result"
            )
        return result

    async def stage_scene(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/scene",
            json=request,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(
                "Integrated scene staging returned a non-object result"
            )
        return result

    async def release_transit_path(self) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/path-release",
            json={},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(
                "Integrated path release returned a non-object result"
            )
        return result

    async def close(self) -> None:
        await self._client.aclose()
