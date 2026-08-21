from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
import threading
import time
import uuid

from .http_client import JsonHttpClient


@dataclass
class BasicLease:
    lease_id: str
    fencing_generation: int
    resource_id: str
    expires_monotonic: float
    required_command_mode: str | None = None


class BasicControllerClient:
    def __init__(self, base_url: str, *, timeout_s: float = 1.5):
        self.base_url = base_url.rstrip("/")
        self.http = JsonHttpClient(timeout_s)
        self.resource_id: str | None = None
        self.joint_index: int | None = None
        self._lock = threading.RLock()
        self.lease: BasicLease | None = None

    def model(self) -> dict[str, Any]:
        return self.http.get(f"{self.base_url}/v1/arm/model")

    def assembly(self) -> dict[str, Any]:
        return self.http.get(f"{self.base_url}/v1/arm/assembly")

    def state(self) -> dict[str, Any]:
        return self.http.get(f"{self.base_url}/v1/arm/state")

    def bind_resource(self, resource_id: str, joint_index: int) -> None:
        normalized = str(resource_id).strip()
        if not normalized:
            raise ValueError("gripper resource_id must be non-empty")
        with self._lock:
            if self.lease is not None:
                raise RuntimeError("cannot change gripper resource while leased")
            self.resource_id = normalized
            self.joint_index = int(joint_index)

    def lease_snapshot(self) -> BasicLease | None:
        with self._lock:
            return None if self.lease is None else replace(self.lease)

    def acquire(self, holder: str, duration_ms: int) -> BasicLease:
        if self.resource_id is None:
            raise RuntimeError("gripper Basic resource is not bound")
        data = self.http.post(
            f"{self.base_url}/v1/control/lease",
            {
                "holder": str(holder),
                "duration_ms": int(duration_ms),
                "resource_id": self.resource_id,
            },
        )
        lease = BasicLease(
            str(data["lease_id"]),
            int(data["fencing_generation"]),
            str(data.get("resource_id") or self.resource_id),
            time.monotonic()
            + int(data.get("expires_in_ms", duration_ms)) / 1000.0,
            (
                str(data["required_command_mode"])
                if data.get("required_command_mode") is not None
                else None
            ),
        )
        with self._lock:
            self.lease = lease
        return replace(lease)

    def renew(self, duration_ms: int) -> BasicLease:
        lease = self.lease_snapshot()
        if lease is None:
            raise RuntimeError("no gripper Basic lease")
        if self.joint_index is None:
            raise RuntimeError("gripper Basic joint is not bound")
        data = self.http.post(
            f"{self.base_url}/v1/control/lease/renew",
            {
                "lease_id": lease.lease_id,
                "fencing_generation": lease.fencing_generation,
                "duration_ms": int(duration_ms),
                "resource_id": lease.resource_id,
            },
        )
        with self._lock:
            if self.lease is None or self.lease.lease_id != lease.lease_id:
                raise RuntimeError("gripper Basic lease changed during renewal")
            self.lease.expires_monotonic = (
                time.monotonic()
                + int(data.get("expires_in_ms", duration_ms)) / 1000.0
            )
            self.lease.required_command_mode = (
                str(data["required_command_mode"])
                if data.get("required_command_mode") is not None
                else None
            )
            return replace(self.lease)

    def command(self, mode: str, values: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        lease = self.lease_snapshot()
        if lease is None:
            raise RuntimeError("no gripper Basic lease")
        return self.http.post(
            f"{self.base_url}/v1/control/command",
            {
                "command_id": str(uuid.uuid4()),
                "lease_id": lease.lease_id,
                "fencing_generation": lease.fencing_generation,
                "resource_id": lease.resource_id,
                "timeout_ms": int(timeout_ms),
                "commands": [
                    {
                        "joint_index": self.joint_index,
                        "mode": str(mode),
                        "values": dict(values),
                    }
                ],
            },
        )

    def set_required_command_mode(self, mode: str | None) -> BasicLease:
        lease = self.lease_snapshot()
        if lease is None:
            raise RuntimeError("no gripper Basic lease")
        data = self.http.post(
            f"{self.base_url}/v1/control/lease/mode-guard",
            {
                "lease_id": lease.lease_id,
                "fencing_generation": lease.fencing_generation,
                "resource_id": lease.resource_id,
                "required_command_mode": mode,
            },
        )
        with self._lock:
            if self.lease is None or self.lease.lease_id != lease.lease_id:
                raise RuntimeError("gripper Basic lease changed during mode guard update")
            self.lease.required_command_mode = (
                str(data["required_command_mode"])
                if data.get("required_command_mode") is not None
                else None
            )
            return replace(self.lease)

    def float(self, reason: str) -> dict[str, Any]:
        return self.http.post(
            f"{self.base_url}/v1/control/request",
            {
                "action": "gravity_float",
                "payload": {"reason": str(reason), "resource_id": self.resource_id},
            },
        )

    def release(self, reason: str) -> None:
        lease = self.lease_snapshot()
        if lease is None:
            return
        try:
            self.http.post(
                f"{self.base_url}/v1/control/lease/release",
                {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "resource_id": lease.resource_id,
                    "reason": str(reason),
                },
            )
        finally:
            with self._lock:
                if self.lease is not None and self.lease.lease_id == lease.lease_id:
                    self.lease = None
