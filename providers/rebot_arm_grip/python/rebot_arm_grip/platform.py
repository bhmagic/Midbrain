from __future__ import annotations

from typing import Any
import os
import time
import uuid

from .http_client import JsonHttpClient


class PlatformPublisher:
    """Use existing Manager and Fabric generic APIs without changing either."""

    def __init__(
        self,
        provider_id: str,
        provider_type: str,
        manager_url: str | None,
        fabric_url: str | None,
        control_url: str,
    ):
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.manager_url = manager_url.rstrip("/") if manager_url else None
        self.fabric_url = fabric_url.rstrip("/") if fabric_url else None
        self.control_url = control_url
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.http = JsonHttpClient(2.0)
        self.sequence = 0
        self.manager_error: str | None = None
        self.fabric_error: str | None = None

    def status(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "residency": state.get("residency", "WARM"),
            "health": state.get("health", "UNKNOWN"),
            "ready": bool(state.get("ready", False)),
            "pid": os.getpid(),
            "control_url": self.control_url,
            "details": state,
        }

    def register(self, state: dict[str, Any]) -> None:
        if not self.manager_url:
            return
        response = self.http.post(
            f"{self.manager_url}/v1/providers/register", self.status(state)
        )
        if response.get("accepted") is False:
            raise RuntimeError("Manager rejected Grip Provider registration")
        self.manager_error = None

    def heartbeat(self, state: dict[str, Any]) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/heartbeat", self.status(state)
            )
            if response.get("accepted") is False:
                raise RuntimeError("Manager rejected Grip Provider heartbeat")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None

    def publish(self, state: dict[str, Any]) -> None:
        if not self.fabric_url:
            return
        self.sequence += 1
        observation = {
            "schema": "midbrain.grip_control_state",
            "schema_version": 1,
            "stream": "robot_effector.grip_control.state",
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": time.time_ns() // 1000,
            "freshness_ms": 250,
            "frame_id": "rebot_arm_grip_center",
            "coordinate_frame": "RIGHT_HANDED_Z_UP",
            "calibration_revision": str(
                state.get("assembly_fingerprint") or "UNKNOWN"
            ),
            "clock_domain": "system_wall_clock",
            "valid": bool(state.get("ready", False)),
            "data": state,
        }
        try:
            self.http.post(f"{self.fabric_url}/v1/observations", observation)
        except Exception as exc:
            self.fabric_error = str(exc)
            raise
        self.fabric_error = None

    def errors(self) -> dict[str, str | None]:
        return {"manager": self.manager_error, "fabric_publish": self.fabric_error}
