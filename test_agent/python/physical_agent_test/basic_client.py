from __future__ import annotations

import uuid
from typing import Any

import httpx


class BasicControllerClient:
    def __init__(self, base_url: str, *, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def state(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/arm/state")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Basic arm state returned a non-object result")
        return result

    async def safe_home(self) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/control/request",
            json={
                "action": "safe_home",
                "request_id": str(uuid.uuid4()),
                "payload": {},
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Basic safe-home returned a non-object result")
        return result

    async def close(self) -> None:
        await self._client.aclose()
