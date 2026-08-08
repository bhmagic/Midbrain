from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class FabricClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=10.0)

    async def health(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def latest(self, stream: str) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/latest/{quote(stream, safe='')}")
        response.raise_for_status()
        return response.json()

    async def latest_optional(self, stream: str) -> dict[str, Any] | None:
        response = await self._client.get(f"{self.base_url}/v1/latest/{quote(stream, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def snapshot(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/snapshot")
        response.raise_for_status()
        return response.json()

    async def streams(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/streams")
        response.raise_for_status()
        return response.json()

    async def transforms(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/transforms")
        response.raise_for_status()
        return response.json()

    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/observations",
            json=observation,
        )
        response.raise_for_status()
        return response.json()

    async def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int | None = None,
        max_extrapolation_us: int = 500_000,
        session_epoch: str | None = None,
        wait_for_bracket_ms: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "max_extrapolation_us": max_extrapolation_us,
        }
        if at_us is not None:
            params["at_us"] = at_us
        if session_epoch is not None:
            params["session_epoch"] = session_epoch
        if wait_for_bracket_ms is not None:
            params["wait_for_bracket_ms"] = max(0, int(wait_for_bracket_ms))
        response = await self._client.get(
            f"{self.base_url}/v1/transform",
            params=params,
            timeout=max(
                10.0,
                (float(wait_for_bracket_ms or 0) / 1000.0) + 1.0,
            ),
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()
