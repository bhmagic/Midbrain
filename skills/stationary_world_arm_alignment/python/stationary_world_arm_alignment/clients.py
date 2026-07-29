from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx


class ManagerClient:
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

    async def workcell_calibrations(self) -> dict[str, Any]:
        response = await self.http.get(
            f"{self.base_url}/v1/workcell-calibrations"
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"activations": []}

    async def set_hot(self, provider_id: str) -> dict[str, Any]:
        encoded = quote(provider_id, safe="")
        response = await self.http.post(f"{self.base_url}/v1/providers/{encoded}/hot")
        response.raise_for_status()
        return response.json()

    async def start_provider(self, provider_id: str) -> dict[str, Any]:
        encoded = quote(provider_id, safe="")
        response = await self.http.post(f"{self.base_url}/v1/providers/{encoded}/start")
        response.raise_for_status()
        return response.json()

    async def ensure_hot(
        self,
        provider_id: str,
        *,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        start_result = await self.start_provider(provider_id)
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                hot_result = await self.set_hot(provider_id)
                return {"start": start_result, "hot": hot_result}
            except (httpx.HTTPError, RuntimeError) as error:
                last_error = error
                await asyncio.sleep(0.5)
        raise RuntimeError(
            f"provider did not accept HOT residency before timeout: {provider_id}: {last_error}"
        )

    async def stop_provider(self, provider_id: str) -> dict[str, Any]:
        encoded = quote(provider_id, safe="")
        response = await self.http.post(f"{self.base_url}/v1/providers/{encoded}/stop")
        response.raise_for_status()
        return response.json()

    async def provider_request(
        self,
        provider_id: str,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        request_id: str | None = None,
        related_skill_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/providers/{quote(provider_id, safe='')}/request",
            json={
                "action": action,
                "payload": payload or {},
                "request_id": request_id,
                "related_skill_id": related_skill_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def acquire_motion_inhibit(
        self,
        *,
        owner_id: str,
        reason: str,
        related_skill_id: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/motion/inhibit/acquire",
            json={
                "owner_id": owner_id,
                "reason": reason,
                "related_skill_id": related_skill_id,
                "duration_ms": duration_ms,
            },
        )
        response.raise_for_status()
        return response.json()

    async def renew_motion_inhibit(
        self,
        *,
        owner_id: str,
        lease_id: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/motion/inhibit/renew",
            json={
                "owner_id": owner_id,
                "lease_id": lease_id,
                "duration_ms": duration_ms,
            },
        )
        response.raise_for_status()
        return response.json()

    async def release_motion_inhibit(
        self,
        *,
        owner_id: str,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"owner_id": owner_id}
        if lease_id:
            payload["lease_id"] = lease_id
        response = await self.http.post(
            f"{self.base_url}/v1/motion/inhibit/release",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def motion_inhibit_status(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/v1/motion/inhibit")
        response.raise_for_status()
        return response.json()

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

    async def recent(self, stream: str, *, limit: int = 64) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"{self.base_url}/v1/recent/{quote(stream, safe='')}",
            params={"limit": limit},
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, list) else value.get("observations", [])

    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/observations",
            json=observation,
        )
        response.raise_for_status()
        return response.json()

    async def publish_batch(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}/v1/observations/batch",
            json={"observations": observations},
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
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "max_extrapolation_us": max_extrapolation_us,
        }
        if at_us is not None:
            params["at_us"] = at_us
        if session_epoch:
            params["session_epoch"] = session_epoch
        response = await self.http.get(f"{self.base_url}/v1/transform", params=params)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.http.aclose()


class FoundationPoseHealthClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=3.0)

    async def health(self) -> dict[str, Any]:
        response = await self.http.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.http.aclose()
