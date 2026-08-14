from __future__ import annotations

from typing import Any
from urllib import error, request
import json


class HttpStatusError(RuntimeError):
    """HTTP response with a non-success status."""

    def __init__(self, status_code: int, body: str, url: str):
        super().__init__(f"HTTP {status_code} from {url}: {body}")
        self.status_code = int(status_code)
        self.body = body
        self.url = url


class JsonHttpClient:
    def __init__(self, timeout: float = 1.5):
        self.timeout = float(timeout)

    def get(self, url: str) -> dict[str, Any]:
        return self._request("GET", url, None)

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", url, payload)

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        operation = request.Request(url, data=data, method=method, headers=headers)
        try:
            with request.urlopen(operation, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise HttpStatusError(exc.code, body, url) from exc
        if not raw:
            return {}
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError(f"JSON response from {url} must be an object")
        return decoded
