from __future__ import annotations

from typing import Any
from urllib.parse import quote
import os
import time
import uuid

from .http_client import HttpStatusError, JsonHttpClient


class PlatformPublisher:
    """Manager heartbeat and Fabric observation adapter."""

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
        self.sequence: dict[str, int] = {}
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
            raise RuntimeError("Manager rejected Contact Provider registration")
        self.manager_error = None

    def heartbeat(self, state: dict[str, Any]) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/heartbeat", self.status(state)
            )
            if response.get("accepted") is False:
                raise RuntimeError("Manager rejected Contact Provider heartbeat")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None

    def motion_inhibit(self) -> dict[str, Any]:
        if not self.manager_url:
            return {"inhibited": False, "owners": [], "enforcement": "NO_MANAGER_URL"}
        try:
            result = self.http.get(f"{self.manager_url}/v1/motion/inhibit")
        except Exception as exc:
            self.manager_error = str(exc)
            raise
        self.manager_error = None
        return result

    def control_authority(self, resource_id: str) -> dict[str, Any]:
        if not self.manager_url:
            return {
                "resource_id": resource_id,
                "enforcement": "NO_MANAGER_URL",
                "active_lease": None,
                "latest_fencing_generation": 0,
            }
        encoded = quote(resource_id, safe="")
        return self.http.get(
            f"{self.manager_url}/v1/control-authority/resources/{encoded}"
        )

    def publish(self, state: dict[str, Any]) -> None:
        if not self.fabric_url:
            return
        stream = "robot_arm.contact_work.state"
        sequence = self.sequence.get(stream, 0) + 1
        self.sequence[stream] = sequence
        observation = {
            "schema": "midbrain.contact_work_state",
            "schema_version": 1,
            "stream": stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": sequence,
            "observed_at_us": int(
                state.get("joint_observed_at_us") or time.time_ns() // 1000
            ),
            "freshness_ms": 250,
            "frame_id": state.get("root_frame_id") or "rebot_arm_base",
            "coordinate_frame": "RIGHT_HANDED_Z_UP",
            "calibration_revision": str(
                state.get("assembly_fingerprint") or "UNKNOWN"
            ),
            "clock_domain": "system_wall_clock",
            "related_skill_id": state.get("skill_id"),
            "valid": bool(state.get("joint_state_valid", False)),
            "data": state,
        }
        try:
            self.http.post(f"{self.fabric_url}/v1/observations", observation)
        except HttpStatusError as exc:
            self.fabric_error = str(exc)
            raise
        except Exception as exc:
            self.fabric_error = str(exc)
            raise
        self.fabric_error = None

    def errors(self) -> dict[str, str | None]:
        return {"manager": self.manager_error, "fabric_publish": self.fabric_error}
