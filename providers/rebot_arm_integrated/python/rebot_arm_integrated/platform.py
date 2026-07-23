from __future__ import annotations

from typing import Any
import os
import time
import uuid
from urllib.parse import quote

from .http_client import HttpStatusError, JsonHttpClient


class PlatformPublisher:
    def __init__(
        self,
        provider_id: str,
        manager_url: str | None,
        fabric_url: str | None,
        control_url: str,
    ):
        self.provider_id = provider_id
        self.manager_url = manager_url.rstrip("/") if manager_url else None
        self.fabric_url = fabric_url.rstrip("/") if fabric_url else None
        self.control_url = control_url
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.http = JsonHttpClient(2.0)
        self.sequence: dict[str, int] = {}
        self.manager_error: str | None = None
        self.fabric_publish_error: str | None = None
        self.fabric_consume_error: str | None = None

    def status(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": "robot_arm.integrated_mit_cartesian_controller",
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
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/register",
                self.status(state),
            )
            if response.get("accepted") is False:
                raise RuntimeError("Midbrain Manager rejected provider registration")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None

    def heartbeat(self, state: dict[str, Any]) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/heartbeat",
                self.status(state),
            )
            if response.get("accepted") is False:
                raise RuntimeError("Midbrain Manager rejected provider heartbeat")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None

    def motion_inhibit(self) -> dict[str, Any]:
        if not self.manager_url:
            return {"inhibited": False, "owners": [], "enforcement": "NO_MANAGER_URL"}
        try:
            payload = self.http.get(f"{self.manager_url}/v1/motion/inhibit")
            if not isinstance(payload, dict):
                raise ValueError("Midbrain motion-inhibit response must be an object")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None
        return payload

    def publish(
        self,
        stream: str,
        schema: str,
        data: dict[str, Any],
        frame_id: str | None = "rebot_arm_base",
        freshness_ms: int = 250,
    ) -> None:
        if not self.fabric_url:
            return
        sequence = self.sequence.get(stream, 0) + 1
        self.sequence[stream] = sequence
        observation = {
            "schema": schema,
            "schema_version": 1,
            "stream": stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": sequence,
            "observed_at_us": time.time_ns() // 1000,
            "freshness_ms": freshness_ms,
            "frame_id": frame_id,
            "coordinate_frame": "RIGHT_HANDED_Z_UP",
            "calibration_revision": "runtime",
            "clock_domain": "system_wall_clock",
            "related_skill_id": None,
            "valid": True,
            "data": data,
        }
        try:
            self.http.post(f"{self.fabric_url}/v1/observations", observation)
        except Exception as exc:
            self.fabric_publish_error = str(exc)
            raise
        self.fabric_publish_error = None

    def latest(self, stream: str) -> dict[str, Any] | None:
        if not self.fabric_url:
            return None
        encoded = quote(str(stream), safe="")
        try:
            payload = self.http.get(f"{self.fabric_url}/v1/latest/{encoded}")
        except HttpStatusError as exc:
            if exc.status_code == 404:
                self.fabric_consume_error = None
                return None
            self.fabric_consume_error = str(exc)
            raise
        except Exception as exc:
            self.fabric_consume_error = str(exc)
            raise
        if not isinstance(payload, dict):
            self.fabric_consume_error = "Fabric latest response must be an object"
            raise ValueError(self.fabric_consume_error)
        self.fabric_consume_error = None
        return payload

    def errors(self) -> dict[str, str | None]:
        return {
            "manager": self.manager_error,
            "fabric_publish": self.fabric_publish_error,
            "fabric_consume": self.fabric_consume_error,
        }
