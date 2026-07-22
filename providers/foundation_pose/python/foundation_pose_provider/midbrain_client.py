"""Provider-local clients used by the FoundationPose tracking GUI."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import cv2
import httpx
import numpy as np


CAMERA_PROVIDER_ID = "camera.femto_bolt"
POSE_PROVIDER_ID = "perception.object_pose.foundation_pose"


def normalized_windows_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Collapse case-duplicate variables before invoking Windows PowerShell."""
    values = source or os.environ
    grouped: dict[str, list[tuple[str, str]]] = {}
    for key, value in values.items():
        grouped.setdefault(key.casefold(), []).append((key, value))
    result: dict[str, str] = {}
    for folded, entries in grouped.items():
        if folded == "path":
            path_parts: list[str] = []
            seen: set[str] = set()
            for _key, value in entries:
                for part in value.split(os.pathsep):
                    normalized = part.strip().casefold()
                    if part.strip() and normalized not in seen:
                        seen.add(normalized)
                        path_parts.append(part.strip())
            result["Path"] = os.pathsep.join(path_parts)
        else:
            result[entries[0][0]] = entries[-1][1]
    return result


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


class CameraFrameReader:
    def __init__(self) -> None:
        self.readers: dict[str, Any] = {}

    def close(self) -> None:
        for reader in self.readers.values():
            try:
                reader.close()
            except Exception:
                pass
        self.readers.clear()

    def read_rgb(self, reference: dict[str, Any]) -> np.ndarray:
        payload = self._read_reference(reference)
        format_name = str(reference.get("format_name", "")).upper()
        width = int(reference["width"])
        height = int(reference["height"])
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise RuntimeError("OpenCV could not decode the camera JPEG frame")
            return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
        expected = width * height
        if format_name == "RGB":
            return np.frombuffer(payload, dtype=np.uint8, count=expected * 3).reshape(height, width, 3).copy()
        if format_name == "BGR":
            image = np.frombuffer(payload, dtype=np.uint8, count=expected * 3).reshape(height, width, 3)
            return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        if format_name == "RGBA":
            image = np.frombuffer(payload, dtype=np.uint8, count=expected * 4).reshape(height, width, 4)
            return cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
        if format_name == "BGRA":
            image = np.frombuffer(payload, dtype=np.uint8, count=expected * 4).reshape(height, width, 4)
            return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
        raise RuntimeError(f"unsupported camera RGB format: {format_name or 'unknown'}")

    def _read_reference(self, reference: dict[str, Any]) -> bytes:
        mapping_name = str(reference.get("mapping_name") or "")
        if not mapping_name:
            raise RuntimeError("camera BufferRef does not include mapping_name")
        reader = self.readers.get(mapping_name)
        if reader is None:
            from orbbec_femto_provider.shared_memory_access import CameraSharedMemory

            reader = CameraSharedMemory(mapping_name).open()
            self.readers[mapping_name] = reader
        return reader.read_ref(reference)


class MidbrainClient:
    def __init__(
        self,
        workspace: Path,
        *,
        manager_url: str = "http://127.0.0.1:7001",
        fabric_url: str = "http://127.0.0.1:7002",
    ) -> None:
        self.workspace = workspace.resolve()
        self.manager_url = manager_url.rstrip("/")
        self.fabric_url = fabric_url.rstrip("/")
        self.http = httpx.Client(timeout=10.0)
        self.frame_reader = CameraFrameReader()

    def close(self) -> None:
        self.frame_reader.close()
        self.http.close()

    def _run_workspace_script(self, name: str) -> str:
        script = self.workspace / "platform_core" / "scripts" / name
        if not script.exists():
            raise FileNotFoundError(f"Midbrain workspace script is missing: {script}")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        if name == "run_workspace.ps1":
            command.append("-NoBrowser")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = subprocess.run(
            command,
            cwd=self.workspace,
            env=normalized_windows_environment() if os.name == "nt" else None,
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=creationflags,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown PowerShell failure").strip()
            raise RuntimeError(f"{name} failed: {message}")
        return result.stdout.strip()

    def start_workspace(self) -> str:
        output = self._run_workspace_script("run_workspace.ps1")
        self.wait_health(self.fabric_url, timeout_s=30.0)
        self.wait_health(self.manager_url, timeout_s=30.0)
        return output

    def stop_workspace(self) -> str:
        return self._run_workspace_script("stop_workspace.ps1")

    def wait_health(self, base_url: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = self.http.get(f"{base_url.rstrip('/')}/health", timeout=2.0)
                response.raise_for_status()
                return response.json()
            except Exception as error:
                last_error = error
                time.sleep(0.25)
        raise RuntimeError(f"timed out waiting for {base_url}: {last_error}")

    def start_provider(self, provider_id: str, timeout_s: float = 60.0) -> None:
        encoded = quote(provider_id, safe="")
        response = self.http.post(f"{self.manager_url}/v1/providers/{encoded}/start", timeout=30.0)
        response.raise_for_status()
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = self.http.post(
                    f"{self.manager_url}/v1/providers/{encoded}/hot",
                    timeout=30.0,
                )
                response.raise_for_status()
                return
            except Exception as error:
                last_error = error
                time.sleep(0.5)
        raise RuntimeError(f"provider did not reach HOT: {provider_id}: {last_error}")

    def start_tracking_stack(self) -> None:
        self.start_provider(CAMERA_PROVIDER_ID, timeout_s=45.0)
        self.start_provider(POSE_PROVIDER_ID, timeout_s=60.0)

    def pose_request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = quote(POSE_PROVIDER_ID, safe="")
        response = self.http.post(
            f"{self.manager_url}/v1/providers/{encoded}/request",
            json={
                "action": action,
                "payload": payload,
                "request_id": str(uuid.uuid4()),
                "related_skill_id": "foundation-pose-bbox-gui",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def latest(self, stream: str) -> dict[str, Any] | None:
        response = self.http.get(
            f"{self.fabric_url}/v1/latest/{quote(stream, safe='')}",
            timeout=3.0,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def recent(self, stream: str, limit: int = 32) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.fabric_url}/v1/recent/{quote(stream, safe='')}",
            params={"limit": limit},
            timeout=3.0,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, list) else []

    def camera_frame(self) -> tuple[np.ndarray, int] | None:
        observation = self.latest("camera.rgbd.bundle")
        data = observation.get("data") if observation else None
        reference = data.get("rgb") if isinstance(data, dict) else None
        if not isinstance(reference, dict):
            return None
        return self.frame_reader.read_rgb(reference), int(reference.get("frame_number", -1))

    def calibration(self) -> dict[str, Any] | None:
        observation = self.latest("camera.calibration")
        data = observation.get("data") if observation else None
        return data if isinstance(data, dict) else None

    def latest_pose_by_model(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for observation in self.recent("perception.object.pose", limit=64):
            data = observation.get("data")
            if isinstance(data, dict) and data.get("model_id"):
                result[str(data["model_id"])] = data
        return result
