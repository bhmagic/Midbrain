from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx


class ManagerLifecycleClient:
    """Manager client intentionally limited to health and Provider lifecycle."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=35.0)

    async def health(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def providers(self) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.base_url}/v1/providers")
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, list) else value.get("providers", [])

    async def start_provider(self, provider_id: str) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/providers/{quote(provider_id, safe='')}/start"
        )
        response.raise_for_status()
        return response.json()

    async def set_hot(self, provider_id: str) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/providers/{quote(provider_id, safe='')}/hot"
        )
        response.raise_for_status()
        return response.json()

    async def stop_provider(self, provider_id: str) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/providers/{quote(provider_id, safe='')}/stop"
        )
        response.raise_for_status()
        return response.json()

    async def ensure_hot(self, provider_id: str, *, timeout_s: float) -> dict[str, Any]:
        start = await self.start_provider(provider_id)
        deadline = time.monotonic() + float(timeout_s)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                hot = await self.set_hot(provider_id)
                return {"start": start, "hot": hot}
            except httpx.HTTPError as error:
                last_error = error
                await asyncio.sleep(0.5)
        raise RuntimeError(f"Provider did not become HOT: {provider_id}: {last_error}")

    async def close(self) -> None:
        await self.http.aclose()


class FabricClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=15.0)

    async def health(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def latest_optional(self, stream: str) -> dict[str, Any] | None:
        response = await self.http.get(f"{self.base_url}/v1/latest/{quote(stream, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        max_extrapolation_us: int,
        session_epoch: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "at_us": int(at_us),
            "max_extrapolation_us": int(max_extrapolation_us),
        }
        if session_epoch:
            params["session_epoch"] = session_epoch
        response = await self.http.get(f"{self.base_url}/v1/transform", params=params)
        response.raise_for_status()
        return response.json()

    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(f"{self.base_url}/v1/observations", json=observation)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.http.aclose()


class IntegratedControlClient:
    """Operator-supervised client for the Integrated provider's published API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=3.0)
        )

    async def health(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def state(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/v1/state")
        response.raise_for_status()
        return response.json()

    async def capabilities(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/v1/capabilities")
        response.raise_for_status()
        return response.json()

    async def settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/settings",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def engage(self, enabled: bool) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/engage",
            json={"enabled": bool(enabled)},
        )
        response.raise_for_status()
        return response.json()

    async def teleop(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/teleop",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def preview(self) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/preview",
            json={
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            },
        )
        response.raise_for_status()
        return response.json()

    async def plan_transit_path_shadow(
        self,
        *,
        target_position_m: list[float],
        target_rpy_rad: list[float],
        requested_speed_m_s: float,
        related_skill_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/motion/path-plan",
            json={
                "command_id": command_id,
                "related_skill_id": related_skill_id,
                "target": {
                    "position_m": list(target_position_m),
                    "rpy_rad": list(target_rpy_rad),
                },
                "requested_speed_m_s": float(requested_speed_m_s),
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            },
        )
        response.raise_for_status()
        return response.json()

    async def request_float(self) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/float",
            json={},
        )
        response.raise_for_status()
        return response.json()

    async def safe_terminate(self) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/safe-terminate",
            json={},
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.http.aclose()
