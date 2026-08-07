from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


def _manager_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    text = response.text.strip()
    return text if text else response.reason_phrase


class ManagerClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def health(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def providers(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/providers")
        response.raise_for_status()
        return response.json()

    async def ui_overview(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/ui/overview")
        response.raise_for_status()
        return response.json()

    async def capabilities(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/capabilities")
        response.raise_for_status()
        return response.json()

    async def bind_capabilities(
        self,
        required_capabilities: list[str],
        *,
        fallback_provider_ids: dict[str, str] | None = None,
        allowed_provider_ids: list[str] | None = None,
        excluded_provider_ids: list[str] | None = None,
        request_id: str | None = None,
        related_skill_id: str | None = None,
    ) -> dict[str, Any]:
        """Request an advisory capability binding without changing provider authority."""
        response = await self._client.post(
            f"{self.base_url}/v1/capability-bindings",
            json={
                "required_capabilities": required_capabilities,
                "fallback_provider_ids": fallback_provider_ids or {},
                "allowed_provider_ids": allowed_provider_ids or [],
                "excluded_provider_ids": excluded_provider_ids or [],
                "request_id": request_id,
                "related_skill_id": related_skill_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def capability_binding(self, binding_id: str) -> dict[str, Any]:
        """Revalidate a binding against the current provider instance and boot."""
        response = await self._client.get(
            f"{self.base_url}/v1/capability-bindings/{binding_id}"
        )
        response.raise_for_status()
        return response.json()

    async def set_hot(self, provider_id: str) -> dict[str, Any]:
        response = await self._client.post(f"{self.base_url}/v1/providers/{provider_id}/hot")
        response.raise_for_status()
        return response.json()

    async def set_residency(
        self,
        provider_id: str,
        action: str,
    ) -> dict[str, Any]:
        normalized = action.strip().lower()
        if normalized not in {"start", "hot", "warm", "stop"}:
            raise ValueError("action must be start, hot, warm, or stop")
        response = await self._client.post(
            f"{self.base_url}/v1/providers/{provider_id}/{normalized}"
        )
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
        response = await self._client.post(
            f"{self.base_url}/v1/providers/{provider_id}/request",
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
        related_skill_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/inhibit/acquire",
            json={
                "owner_id": owner_id,
                "reason": reason,
                "related_skill_id": related_skill_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def release_motion_inhibit(self, *, owner_id: str) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/motion/inhibit/release",
            json={"owner_id": owner_id},
        )
        response.raise_for_status()
        return response.json()

    async def motion_inhibit_status(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/motion/inhibit")
        response.raise_for_status()
        return response.json()

    async def workcell_calibrations(self) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/v1/workcell-calibrations"
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"activations": []}

    async def activate_workcell_calibration(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/workcell-calibrations/activate",
            json=request,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                "Manager rejected workcell calibration activation "
                f"({response.status_code}): {_manager_error_detail(response)}"
            ) from error
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(
                "Manager returned a non-object calibration activation"
            )
        return value

    async def revoke_workcell_calibration(
        self,
        activation_id: str,
        *,
        request_id: str,
        revoked_by: str,
        reason: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            (
                f"{self.base_url}/v1/workcell-calibrations/"
                f"{quote(activation_id, safe='')}/revoke"
            ),
            json={
                "request_id": request_id,
                "revoked_by": revoked_by,
                "reason": reason,
            },
        )
        response.raise_for_status()
        return response.json()

    async def refine_workcell_calibration_translation(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/workcell-calibrations/refine-translation",
            json=request,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(
                "Manager translation-refinement response must be an object"
            )
        return value

    async def close(self) -> None:
        await self._client.aclose()
