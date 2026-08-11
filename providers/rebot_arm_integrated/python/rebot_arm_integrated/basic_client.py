from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
import json
import threading
import time
import uuid

from .http_client import HttpStatusError, JsonHttpClient


class LeaseLostError(RuntimeError):
    """The Basic Controller fenced, expired, or rejected the current lease."""

    def __init__(self, reason: str, error_code: str = "LEASE_REJECTED", details: dict[str, Any] | None = None):
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
    """Client for the Basic Controller operational control API v2.

    Lease renewal, state polling, commands, and safety requests use independent
    HTTP clients so a slow data request cannot starve the lease renewal path.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 1.5,
        *,
        state_timeout: float = 1.2,
        command_timeout: float = 1.5,
        lease_timeout: float = 1.5,
        safety_timeout: float = 3.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.http = JsonHttpClient(timeout)
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
            parsed = json.loads(exc.body)
            if isinstance(parsed, dict):
                details = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        reason = str(details.get("reason") or details.get("error") or exc)
        code = str(details.get("error_code") or "LEASE_REJECTED")
        return LeaseLostError(reason, code, details)

    def lease_snapshot(self) -> BasicLease | None:
        with self._lease_lock:
            return None if self.lease is None else replace(self.lease)

    def clear_lease(self, lease_id: str | None = None, generation: int | None = None) -> None:
        with self._lease_lock:
            if self.lease is None:
                return
            if lease_id is not None and self.lease.lease_id != lease_id:
                return
            if generation is not None and self.lease.fencing_generation != generation:
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
                    "duration_ms": duration_ms,
                    "resource_id": self.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code == 404:
                raise LeaseLostError(
                    "Basic Controller operational API v2 is unavailable; install rebot_arm_dm 0.1.20 or newer",
                    "OPERATIONAL_API_UNAVAILABLE",
                ) from exc
            if exc.status_code in {403, 409}:
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
            raise LeaseLostError("no Basic Controller lease", "NO_LOCAL_LEASE")
        try:
            data = self.lease_http.post(
                f"{self.base_url}/v1/control/lease/renew",
                {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "duration_ms": duration_ms,
                    "resource_id": lease.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                self.clear_lease(lease.lease_id, lease.fencing_generation)
                raise self._lease_error(exc) from exc
            raise
        renewed_expiry = time.monotonic() + int(data.get("expires_in_ms", duration_ms)) / 1000.0
        with self._lease_lock:
            if (
                self.lease is None
                or self.lease.lease_id != lease.lease_id
                or self.lease.fencing_generation != lease.fencing_generation
            ):
                raise LeaseLostError("lease changed while renewal was in flight", "LOCAL_LEASE_CHANGED")
            self.lease.expires_monotonic = renewed_expiry
            return replace(self.lease)

    def command(self, commands: list[dict[str, Any]], timeout_ms: int = 250) -> dict[str, Any]:
        lease = self.lease_snapshot()
        if lease is None:
            raise LeaseLostError("no Basic Controller lease", "NO_LOCAL_LEASE")
        try:
            return self.command_http.post(
                f"{self.base_url}/v1/control/command",
                {
                    "command_id": str(uuid.uuid4()),
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "timeout_ms": timeout_ms,
                    "resource_id": lease.resource_id,
                    "commands": commands,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                self.clear_lease(lease.lease_id, lease.fencing_generation)
                raise self._lease_error(exc) from exc
            raise


    def set_payload(self, mass_kg: float, com_tool_m: list[float]) -> dict[str, Any]:
        lease = self.lease_snapshot()
        if lease is None:
            raise LeaseLostError("no Basic Controller lease", "NO_LOCAL_LEASE")
        try:
            return self.command_http.post(
                f"{self.base_url}/v1/control/payload",
                {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "mass_kg": float(mass_kg),
                    "com_tool_m": [float(value) for value in com_tool_m],
                    "resource_id": lease.resource_id,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code in {403, 404, 409}:
                self.clear_lease(lease.lease_id, lease.fencing_generation)
                raise self._lease_error(exc) from exc
            raise

    def float(self, reason: str = "integrated controller idle") -> dict[str, Any]:
        return self.safety_http.post(
            f"{self.base_url}/v1/control/request",
            {
                "action": "gravity_float",
                "payload": {
                    "reason": reason,
                    "resource_id": self.resource_id,
                },
            },
        )

    def safe_home_stop(self) -> dict[str, Any]:
        return self.safety_http.post(f"{self.base_url}/v1/control/stop", {})

    def release(self, reason: str = "integrated controller release") -> None:
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
            if exc.status_code in {403, 404, 409}:
                raise self._lease_error(exc) from exc
            raise
        finally:
            self.clear_lease(lease.lease_id, lease.fencing_generation)
