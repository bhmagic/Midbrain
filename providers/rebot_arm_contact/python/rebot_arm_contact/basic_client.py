from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
import json
import threading
import time
import uuid

from .http_client import HttpStatusError, JsonHttpClient


class LeaseLostError(RuntimeError):
    """Basic fenced, expired, or rejected the Contact Provider lease."""

    def __init__(
        self,
        reason: str,
        error_code: str = "LEASE_REJECTED",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.error_code = error_code
        self.details = details or {}


@dataclass
class BasicLease:
    lease_id: str
    fencing_generation: int
    expires_monotonic: float
    holder: str
    resource_id: str | None = None


class BasicControllerClient:
    """Independent client for Basic operational control API v2."""

    def __init__(
        self,
        base_url: str,
        *,
        state_timeout: float = 1.2,
        command_timeout: float = 1.5,
        lease_timeout: float = 1.5,
        safety_timeout: float = 3.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.state_http = JsonHttpClient(state_timeout)
        self.command_http = JsonHttpClient(command_timeout)
        self.lease_http = JsonHttpClient(lease_timeout)
        self.safety_http = JsonHttpClient(safety_timeout)
        self._lease_lock = threading.RLock()
        self.lease: BasicLease | None = None
        self.resource_id: str | None = None

    @staticmethod
    def _lease_error(exc: HttpStatusError) -> LeaseLostError:
        details: dict[str, Any] = {}
        try:
            decoded = json.loads(exc.body)
            if isinstance(decoded, dict):
                details = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return LeaseLostError(
            str(details.get("reason") or details.get("error") or exc),
            str(details.get("error_code") or "LEASE_REJECTED"),
            details,
        )

    def lease_snapshot(self) -> BasicLease | None:
        with self._lease_lock:
            return None if self.lease is None else replace(self.lease)

    def clear_lease(self, lease_id: str | None = None) -> None:
        with self._lease_lock:
            if self.lease is None:
                return
            if lease_id is not None and self.lease.lease_id != lease_id:
                return
            self.lease = None

    def health(self) -> dict[str, Any]:
        return self.state_http.get(f"{self.base_url}/health")

    def state(self) -> dict[str, Any]:
        return self.state_http.get(f"{self.base_url}/v1/arm/state")

    def model(self) -> dict[str, Any]:
        return self.state_http.get(f"{self.base_url}/v1/arm/model")

    def assembly(self) -> dict[str, Any]:
        return self.state_http.get(f"{self.base_url}/v1/arm/assembly")

    def bind_resource(self, resource_id: str) -> None:
        normalized = str(resource_id).strip()
        if not normalized:
            raise ValueError("Basic control resource_id must be non-empty")
        with self._lease_lock:
            if self.lease is not None:
                raise RuntimeError("cannot change Basic resource while leased")
            self.resource_id = normalized

    def acquire(self, holder: str, duration_ms: int) -> BasicLease:
        try:
            data = self.lease_http.post(
                f"{self.base_url}/v1/control/lease",
                {
                    "holder": holder,
                    "duration_ms": int(duration_ms),
                    "resource_id": self.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                raise self._lease_error(exc) from exc
            raise
        lease = BasicLease(
            str(data["lease_id"]),
            int(data["fencing_generation"]),
            time.monotonic() + int(data.get("expires_in_ms", duration_ms)) / 1000.0,
            holder,
            str(data.get("resource_id") or self.resource_id or "") or None,
        )
        with self._lease_lock:
            self.lease = lease
        return replace(lease)

    def renew(self, duration_ms: int) -> BasicLease:
        lease = self.lease_snapshot()
        if lease is None:
            raise LeaseLostError("no Basic lease", "NO_LOCAL_LEASE")
        try:
            data = self.lease_http.post(
                f"{self.base_url}/v1/control/lease/renew",
                {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "duration_ms": int(duration_ms),
                    "resource_id": lease.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                self.clear_lease(lease.lease_id)
                raise self._lease_error(exc) from exc
            raise
        with self._lease_lock:
            if self.lease is None or self.lease.lease_id != lease.lease_id:
                raise LeaseLostError("lease changed during renewal", "LOCAL_LEASE_CHANGED")
            self.lease.expires_monotonic = (
                time.monotonic()
                + int(data.get("expires_in_ms", duration_ms)) / 1000.0
            )
            return replace(self.lease)

    def command(
        self,
        commands: list[dict[str, Any]],
        timeout_ms: int,
    ) -> dict[str, Any]:
        lease = self.lease_snapshot()
        if lease is None:
            raise LeaseLostError("no Basic lease", "NO_LOCAL_LEASE")
        try:
            return self.command_http.post(
                f"{self.base_url}/v1/control/command",
                {
                    "command_id": str(uuid.uuid4()),
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "timeout_ms": int(timeout_ms),
                    "resource_id": lease.resource_id,
                    "commands": commands,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                self.clear_lease(lease.lease_id)
                raise self._lease_error(exc) from exc
            raise

    def float(self, reason: str) -> dict[str, Any]:
        return self.safety_http.post(
            f"{self.base_url}/v1/control/request",
            {
                "action": "gravity_float",
                "payload": {"reason": reason, "resource_id": self.resource_id},
            },
        )

    def release(self, reason: str) -> None:
        lease = self.lease_snapshot()
        if lease is None:
            return
        try:
            self.safety_http.post(
                f"{self.base_url}/v1/control/lease/release",
                {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "reason": reason,
                    "resource_id": lease.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code not in {403, 404, 409}:
                raise
        finally:
            self.clear_lease(lease.lease_id)
