from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class FabricClient:
    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.http = httpx.Client(timeout=float(timeout_s))

    def latest_optional(self, stream: str) -> dict[str, Any] | None:
        response = self.http.get(
            f"{self.base_url}/v1/latest/{quote(stream, safe='')}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Fabric latest response must be an object")
        return result

    def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        max_extrapolation_us: int,
    ) -> dict[str, Any]:
        response = self.http.get(
            f"{self.base_url}/v1/transform",
            params={
                "from_frame": from_frame,
                "to_frame": to_frame,
                "at_us": int(at_us),
                "max_extrapolation_us": int(max_extrapolation_us),
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Fabric transform response must be an object")
        return result

    def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{self.base_url}/v1/observations",
            json=observation,
        )
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        self.http.close()
