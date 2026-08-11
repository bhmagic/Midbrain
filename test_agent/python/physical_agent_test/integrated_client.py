from __future__ import annotations

from typing import Any

import httpx


def _raise_with_detail(response: httpx.Response, operation: str) -> None:
    if not response.is_error:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("detail") or "")
    except Exception:
        detail = response.text.strip()
    raise RuntimeError(
        f"{operation} failed ({response.status_code}): "
        f"{detail or response.reason_phrase}"
    )


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
        _raise_with_detail(response, "Integrated path preview")
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
        _raise_with_detail(response, "Integrated path commit")
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

    async def set_idle_profile(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._idle_profile_request("/v1/idle-profile", request)

    async def renew_idle_profile(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._idle_profile_request(
            "/v1/idle-profile/renew",
            request,
        )

    async def release_idle_profile(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._idle_profile_request(
            "/v1/idle-profile/release",
            request,
        )

    async def _idle_profile_request(
        self,
        path: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}{path}",
            json=request,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Integrated idle profile returned a non-object result")
        return result

    async def close(self) -> None:
        await self._client.aclose()
