from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class HttpStatusError(RuntimeError):
    """HTTP failure with a machine-readable status code."""

    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = int(status_code)
        self.url = url
        self.body = body
        super().__init__(f"HTTP {self.status_code} from {url}: {body}")


class JsonHttpClient:
    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout

    def _read(self, req: request.Request) -> dict[str, Any]:
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise HttpStatusError(exc.code, req.full_url, body) from exc
        return {} if not raw else json.loads(raw)

    def get(self, url: str) -> dict[str, Any]:
        return self._read(request.Request(url, method="GET"))

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read(req)
