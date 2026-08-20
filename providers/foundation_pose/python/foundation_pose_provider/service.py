from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any

import httpx

from .backend import EstimateInput, NativeFoundationPoseBackend
from .evidence import load_evidence


PROVIDER_ID = "perception.foundation_pose"
PROVIDER_TYPE = "perception.known_object_pose"
PROVIDER_VERSION = "1.1.2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _intrinsics_matrix(value: Any) -> tuple[float, ...]:
    if isinstance(value, list) and len(value) == 9:
        return tuple(float(item) for item in value)
    if isinstance(value, dict):
        fx, fy = float(value["fx"]), float(value["fy"])
        cx, cy = float(value["cx"]), float(value["cy"])
        return (fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0)
    raise ValueError("camera_intrinsics must be a 3x3 row-major list or fx/fy/cx/cy object")


class FoundationPoseProvider:
    """Persistent generic pose-estimation resource with no robot-role policy."""

    def __init__(
        self,
        config: dict[str, Any],
        root: Path,
        manager_url: str | None,
        fabric_url: str | None,
        control_url: str,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self.manager_url = manager_url.rstrip("/") if manager_url else None
        self.fabric_url = fabric_url.rstrip("/") if fabric_url else None
        self.control_url = control_url
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.http = httpx.Client(timeout=10.0)
        self.backend: NativeFoundationPoseBackend | None = None
        self.residency = "WARM"
        self.health = "HEALTHY"
        self.last_error: str | None = None
        self.manager_error: str | None = None
        self.fabric_error: str | None = None
        self.last_estimate: dict[str, Any] | None = None
        self.sequence = 0
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()

    def status_payload(self) -> dict[str, Any]:
        with self.lock:
            ready = self.residency == "HOT" and self.backend is not None
            return {
                "provider_id": PROVIDER_ID,
                "provider_type": PROVIDER_TYPE,
                "instance_id": self.instance_id,
                "boot_id": self.boot_id,
                "residency": self.residency,
                "health": self.health,
                "ready": ready,
                "pid": os.getpid(),
                "control_url": self.control_url,
                "details": {
                    "provider_version": PROVIDER_VERSION,
                    "architecture_role": "GENERIC_KNOWN_OBJECT_POSE_ESTIMATOR",
                    "last_error": self.last_error,
                    "manager_error": self.manager_error,
                    "fabric_error": self.fabric_error,
                    "last_estimate": self.last_estimate,
                    "capability_readiness": {
                        "perception.known_object_pose.estimate": ready
                    },
                    "resource_profile": {
                        "basis": "IMPLEMENTATION_TARGET",
                        "gpu": "CUDA_12_8_TENSORRT",
                        "hot_advantage": "native CUDA sampling/rendering and resident TensorRT engines",
                    },
                },
            }

    def register(self) -> None:
        if not self.manager_url:
            return
        response = self.http.post(
            f"{self.manager_url}/v1/providers/register", json=self.status_payload()
        )
        response.raise_for_status()
        self.manager_error = None

    def heartbeat(self) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/heartbeat", json=self.status_payload()
            )
            response.raise_for_status()
            self.manager_error = None
        except Exception as exc:
            self.manager_error = str(exc)

    def start_hot(self) -> dict[str, Any]:
        with self.lock:
            if self.backend is not None:
                return {"status": "already_hot"}
            try:
                backend = NativeFoundationPoseBackend(self.config, self.root)
            except Exception as exc:
                self.residency = "WARM"
                self.health = "DEGRADED"
                self.last_error = str(exc)
                raise
            self.backend = backend
            self.residency = "HOT"
            self.health = "HEALTHY"
            self.last_error = None
            return {"status": "hot"}

    def enter_warm(self) -> dict[str, Any]:
        with self.lock:
            backend, self.backend = self.backend, None
            self.residency = "WARM"
            self.health = "HEALTHY"
        if backend:
            backend.close()
        return {"status": "warm"}

    def stop(self) -> dict[str, Any]:
        self.enter_warm()
        self.residency = "STOPPING"
        self.shutdown_event.set()
        return {"status": "stopping"}

    def estimate(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            backend = self.backend
        if self.residency != "HOT" or backend is None:
            raise RuntimeError("pose estimation requires honest HOT residency")
        evidence_spec = request.get("evidence")
        mesh_spec = request.get("mesh")
        if not isinstance(evidence_spec, dict) or not isinstance(mesh_spec, dict):
            raise ValueError("estimate requires evidence and mesh objects")
        frame = load_evidence(evidence_spec)
        mesh_path = Path(str(mesh_spec.get("path") or ""))
        if not mesh_path.is_absolute():
            mesh_path = self.root / mesh_path
        mesh_path = mesh_path.resolve()
        if not mesh_path.is_file():
            raise ValueError(f"CAD mesh is unavailable: {mesh_path}")
        expected_hash = str(mesh_spec.get("sha256") or "").lower()
        actual_hash = _sha256(mesh_path)
        if expected_hash and expected_hash != actual_hash:
            raise ValueError("CAD mesh SHA-256 does not match the request")
        output = backend.estimate(
            EstimateInput(
                rgb=frame.rgb,
                depth_m=frame.depth_m,
                mask=frame.mask,
                intrinsics=_intrinsics_matrix(request.get("camera_intrinsics")),
                mesh_path=mesh_path,
                mesh_scale_to_m=float(mesh_spec.get("scale_to_m") or 1.0),
            )
        )
        observed_at_us = int(request.get("observed_at_us") or time.time_ns() // 1000)
        measurement = {
            "schema": "midbrain.perception.known_object_pose.measurement",
            "schema_version": 1,
            "measurement_id": str(uuid.uuid4()),
            "request_id": str(request.get("request_id") or uuid.uuid4()),
            "observed_at_us": observed_at_us,
            "camera_frame": str(request.get("camera_frame") or "camera_optical_frame"),
            "mesh_asset": {
                "sha256": actual_hash,
                "scale_to_m": float(mesh_spec.get("scale_to_m") or 1.0),
                "centered_mesh": True,
            },
            "camera_from_centered_mesh": output.camera_from_centered_mesh,
            "quality": {
                "score": output.score,
                "ranking_score_raw": output.score,
                "score_semantics": "RAW_MODEL_RANKING_ONLY",
                "absolute_acceptance_threshold_defined": False,
                "hypothesis_count": output.hypothesis_count,
            },
            "timing": {"native_elapsed_ms": output.elapsed_ms},
            "provenance": {
                "provider_id": PROVIDER_ID,
                "provider_instance_id": self.instance_id,
                "provider_version": PROVIDER_VERSION,
                "backend": "WINDOWS_NATIVE_CUDA_TENSORRT",
                "source_evidence": frame.source,
            },
        }
        with self.lock:
            self.last_estimate = {
                "measurement_id": measurement["measurement_id"],
                "observed_at_us": observed_at_us,
                "native_elapsed_ms": output.elapsed_ms,
                "score": output.score,
                "mesh_filename": mesh_path.name,
                "mesh_path": str(mesh_path),
                "mesh_sha256": actual_hash,
                "source_evidence": frame.source,
                "mask_path": str(evidence_spec.get("mask", {}).get("path") or ""),
            }
            self.health = "HEALTHY"
            self.last_error = None
        self._publish(measurement)
        return measurement

    def _publish(self, measurement: dict[str, Any]) -> None:
        if not self.fabric_url:
            return
        self.sequence += 1
        observation = {
            "schema": "midbrain.perception.known_object_pose.observation",
            "schema_version": 1,
            "stream": "perception.known_object_pose.measurement",
            "provider_id": PROVIDER_ID,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": measurement["observed_at_us"],
            "freshness_ms": 5000,
            "frame_id": measurement["camera_frame"],
            "coordinate_frame": "RIGHT_HANDED_CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD",
            "calibration_revision": str(measurement.get("calibration_revision") or "UNKNOWN"),
            "clock_domain": "system_wall_clock",
            "valid": True,
            "data": measurement,
        }
        try:
            response = self.http.post(
                f"{self.fabric_url}/v1/observations", json=observation
            )
            response.raise_for_status()
            self.fabric_error = None
        except Exception as exc:
            self.fabric_error = str(exc)
            raise

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()
        if action == "status":
            return self.status_payload()
        if action == "estimate":
            payload = request.get("payload")
            payload = dict(payload) if isinstance(payload, dict) else dict(request)
            payload.setdefault("request_id", request.get("request_id"))
            return {"status": "completed", "measurement": self.estimate(payload)}
        raise ValueError(f"unsupported FoundationPose action {action or 'empty'!r}")

    def run(self) -> int:
        self.register()
        while not self.shutdown_event.wait(1.0):
            self.heartbeat()
        return 0

    def close(self) -> None:
        if self.residency != "STOPPING":
            self.stop()
        self.http.close()
