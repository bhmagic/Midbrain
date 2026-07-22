"""Windows-first FoundationPose Resource Provider for Midbrain.

The Provider consumes the existing camera RGB-D BufferRef observations directly
from Windows named shared memory. It publishes generic object-pose measurements
and timestamped transforms while keeping NVIDIA-specific runtime details behind
an adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import cv2
import httpx
import numpy as np

PROVIDER_ROOT = Path(__file__).resolve().parent
PYTHON_ROOT = PROVIDER_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from foundation_pose_provider.backend import (  # noqa: E402
    BackendResult,
    FoundationPoseBackend,
    MockFoundationPoseBackend,
    NvLabsFoundationPoseBackend,
)
from foundation_pose_provider.bounding_box import BoundingBoxMask  # noqa: E402
from foundation_pose_provider.math3d import (  # noqa: E402
    diagonal_covariance,
    transform_payload,
)
from foundation_pose_provider.model_registry import (  # noqa: E402
    ObjectModel,
    ObjectModelRegistry,
)

PROVIDER_ID = "perception.object_pose.foundation_pose"
PROVIDER_VERSION = "0.3.0"
DEFAULT_CAMERA_FRAME = "femto_bolt_color_optical_frame"
ACTIVE_STATES = {"WAITING_FOR_INPUTS", "INITIALIZING", "TRACKING", "DEGRADED"}
TERMINAL_STATES = {"COMPLETED", "STOPPED", "FAILED", "EXPIRED"}


@dataclass
class PoseSession:
    """One bounded or continuous object-pose request."""

    session_id: str
    operation: str
    model_id: str
    target_id: str
    child_frame: str
    parent_frame: str
    mask_stream: str
    mask_path: Optional[Path]
    bounding_box: Optional[BoundingBoxMask]
    related_skill_id: Optional[str]
    max_duration_s: float
    max_update_hz: float
    created_monotonic: float = field(default_factory=time.monotonic)
    state: str = "WAITING_FOR_INPUTS"
    initialized: bool = False
    last_frame_number: int = -1
    last_update_monotonic: float = 0.0
    result_count: int = 0
    last_error: Optional[str] = None
    last_observed_at_us: int = 0
    last_latency_ms: Optional[float] = None
    consecutive_failures: int = 0

    def public_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "operation": self.operation,
            "model_id": self.model_id,
            "target_id": self.target_id,
            "parent_frame": self.parent_frame,
            "child_frame": self.child_frame,
            "mask_stream": self.mask_stream,
            "mask_path": str(self.mask_path) if self.mask_path else None,
            "bounding_box": (
                self.bounding_box.public_payload() if self.bounding_box else None
            ),
            "related_skill_id": self.related_skill_id,
            "max_duration_s": self.max_duration_s,
            "max_update_hz": self.max_update_hz,
            "state": self.state,
            "initialized": self.initialized,
            "last_frame_number": self.last_frame_number,
            "result_count": self.result_count,
            "last_error": self.last_error,
            "last_observed_at_us": self.last_observed_at_us,
            "last_latency_ms": self.last_latency_ms,
            "consecutive_failures": self.consecutive_failures,
        }


class FoundationPoseProvider:
    """Midbrain Provider implementation and session scheduler."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.provider_id = PROVIDER_ID
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.shutdown_event = threading.Event()
        self.lock = threading.RLock()
        self.iteration_lock = threading.Lock()
        self.http = httpx.Client(timeout=5.0)
        self.residency = "WARM"
        self.health = "HEALTHY"
        self.ready = False
        self.last_error: Optional[str] = None
        self.manager_error: Optional[str] = None
        self.sequence = 0
        self.registry = ObjectModelRegistry(Path(args.model_registry))
        self.backend = self._create_backend(args)
        self.sessions: dict[str, PoseSession] = {}
        self.cached_inputs: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        self.readers: dict[str, Any] = {}
        self.camera_matrix: Optional[np.ndarray] = None
        self.camera_calibration_revision: Optional[str] = None
        self.camera_frame = DEFAULT_CAMERA_FRAME
        self.last_camera_frame_number = -1
        self.last_status_publish_monotonic = 0.0

    @staticmethod
    def _create_backend(args: argparse.Namespace) -> FoundationPoseBackend:
        if args.backend == "mock":
            return MockFoundationPoseBackend()
        root_value = args.foundationpose_root or os.environ.get("FOUNDATIONPOSE_ROOT")
        if not root_value:
            raise ValueError(
                "--foundationpose-root or FOUNDATIONPOSE_ROOT is required for the nvlabs backend"
            )
        return NvLabsFoundationPoseBackend(
            Path(root_value),
            estimate_iterations=args.estimate_iterations,
            track_iterations=args.track_iterations,
            debug_level=args.debug_level,
            debug_dir=Path(args.debug_dir),
            prepared_model_cache_size=getattr(args, "prepared_model_cache_size", 4),
        )

    def register(self) -> None:
        response = self.http.post(
            f"{self.args.manager_url}/v1/providers/register",
            json=self._status_payload(),
        )
        response.raise_for_status()

    def start_hot(self) -> dict[str, Any]:
        with self.lock:
            already_hot = self.residency == "HOT"
            self.residency = "HOT"
            self.health = "HEALTHY"
            self.ready = True
            self.last_error = None
        self._heartbeat()
        return {
            "status": "already_hot" if already_hot else "hot",
            "backend": self.args.backend,
            "registry_revision": self.registry.revision,
        }

    def enter_warm(self) -> dict[str, Any]:
        with self.iteration_lock:
            with self.lock:
                for session in self.sessions.values():
                    if session.state in ACTIVE_STATES:
                        session.state = "STOPPED"
                        session.last_error = "Provider entered WARM residency"
                    self.backend.reset(session.session_id)
                self.residency = "WARM"
                self.ready = False
                self._close_readers()
        return {"status": "warm"}

    def stop(self) -> dict[str, Any]:
        self.shutdown_event.set()
        with self.lock:
            self.residency = "STOPPING"
            self.ready = False
            for session in self.sessions.values():
                if session.state not in TERMINAL_STATES:
                    session.state = "STOPPED"
                    session.last_error = "Provider stopping"
        return {"status": "stopping"}

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()

        # Midbrain Manager forwards request-specific fields under "payload".
        # Merge them into an effective request while retaining Manager metadata
        # such as request_id and related_skill_id. Direct Provider calls that
        # already use top-level fields remain supported.
        effective_request = {
            key: value
            for key, value in request.items()
            if key != "payload"
        }
        payload = request.get("payload")
        if isinstance(payload, dict):
            effective_request.update(payload)
        effective_request["action"] = action

        if action in {"estimate", "track"}:
            return self._create_session(
                effective_request,
                operation=action.upper(),
            )
        if action == "relocalize":
            return self._relocalize(effective_request)
        if action in {"stop", "stop_tracking", "cancel"}:
            return self._stop_session(effective_request)
        if action == "status":
            session_id = str(
                effective_request.get("session_id") or ""
            ).strip()
            if session_id:
                return self._require_session(session_id).public_payload()
            return self._status_payload()
        if action in {"list_models", "models", "model_registry"}:
            return self.registry.public_payload()
        if action == "reload_models":
            self.registry.reload()
            return self.registry.public_payload()
        raise ValueError(f"unsupported object-pose action: {action or 'empty'}")

    def _create_session(self, request: dict[str, Any], *, operation: str) -> dict[str, Any]:
        if self.residency != "HOT":
            raise RuntimeError("Provider must be HOT before accepting pose sessions")
        model_id = str(request.get("model_id") or "").strip()
        if not model_id:
            raise ValueError("model_id is required")
        model = self.registry.get(model_id, require_mesh=self.args.backend != "mock")
        target_id = str(request.get("target_id") or model_id).strip()
        session_id = str(request.get("session_id") or uuid.uuid4()).strip()
        if not session_id:
            raise ValueError("session_id cannot be empty")
        with self.lock:
            if session_id in self.sessions and self.sessions[session_id].state not in TERMINAL_STATES:
                raise ValueError(f"session already exists: {session_id}")
            requested_child = str(request.get("child_frame") or "").strip()
            child_frame = (
                requested_child
                or model.default_child_frame
                or f"observed_object/{target_id}/{session_id[:8]}"
            )
            parent_frame = str(request.get("parent_frame") or self.camera_frame).strip()
            mask_path_value = str(request.get("mask_path") or "").strip()
            mask_path = Path(mask_path_value).expanduser().resolve() if mask_path_value else None
            bounding_box = BoundingBoxMask.from_request(request)
            max_duration_s = float(
                request.get("max_duration_s")
                or (self.args.default_track_duration_s if operation == "TRACK" else 30.0)
            )
            max_update_hz = float(request.get("max_update_hz") or self.args.default_update_hz)
            if max_duration_s <= 0.0:
                raise ValueError("max_duration_s must be positive")
            if max_update_hz <= 0.0:
                raise ValueError("max_update_hz must be positive")
            session = PoseSession(
                session_id=session_id,
                operation=operation,
                model_id=model.model_id,
                target_id=target_id,
                child_frame=child_frame,
                parent_frame=parent_frame,
                mask_stream=str(request.get("mask_stream") or "perception.object.mask"),
                mask_path=mask_path,
                bounding_box=bounding_box,
                related_skill_id=(
                    str(request.get("related_skill_id"))
                    if request.get("related_skill_id") is not None
                    else None
                ),
                max_duration_s=max_duration_s,
                max_update_hz=max_update_hz,
            )
            self.sessions[session_id] = session
        self._best_effort_status(session, "pose session accepted")
        return session.public_payload()

    def _relocalize(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(str(request.get("session_id") or ""))
        with self.lock:
            self.backend.reset(session.session_id)
            session.initialized = False
            session.state = "WAITING_FOR_INPUTS"
            session.last_error = None
            session.last_frame_number = -1
            session.last_update_monotonic = 0.0
            session.consecutive_failures = 0
            mask_path_value = str(request.get("mask_path") or "").strip()
            if mask_path_value:
                session.mask_path = Path(mask_path_value).expanduser().resolve()
                session.bounding_box = None
            elif "bounding_box" in request or "box_2d" in request:
                session.bounding_box = BoundingBoxMask.from_request(request)
                session.mask_path = None
            if request.get("mask_stream"):
                session.mask_stream = str(request["mask_stream"])
        self._best_effort_status(session, "relocalization requested")
        return session.public_payload()

    def _stop_session(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(str(request.get("session_id") or ""))
        with self.lock:
            self.backend.reset(session.session_id)
            session.state = "STOPPED"
            session.last_error = str(request.get("reason") or "session stopped by request")
        self._best_effort_status(session, session.last_error)
        return session.public_payload()

    def _require_session(self, session_id: str) -> PoseSession:
        if not session_id:
            raise ValueError("session_id is required")
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise KeyError(f"unknown pose session: {session_id}") from error

    def run(self) -> int:
        self.register()
        self.start_hot()
        heartbeat_at = 0.0
        while not self.shutdown_event.is_set():
            now = time.monotonic()
            if now >= heartbeat_at:
                self._heartbeat()
                heartbeat_at = now + 1.0
            if self.residency != "HOT":
                time.sleep(0.1)
                continue
            try:
                with self.iteration_lock:
                    self._iteration()
            except Exception as error:
                self.health = "DEGRADED"
                self.last_error = str(error)
                time.sleep(max(0.02, self.args.poll_interval))
        self._close_readers()
        self.backend.close()
        return 0

    def _iteration(self) -> None:
        sessions = [session for session in self.sessions.values() if session.state in ACTIVE_STATES]
        if not sessions:
            self.ready = True
            self.health = "HEALTHY"
            time.sleep(self.args.poll_interval)
            return

        calibration = self._latest_cached("camera.calibration", refresh_s=1.0)
        bundle_observation = self._latest_optional("camera.rgbd.bundle")
        if calibration is None or bundle_observation is None:
            self._mark_waiting(sessions, "camera calibration or RGB-D bundle unavailable")
            time.sleep(self.args.poll_interval)
            return
        self._configure_calibration(calibration)
        bundle = bundle_observation.get("data") or {}
        rgb_reference = bundle.get("rgb")
        depth_reference = bundle.get("depth_aligned_to_rgb")
        if not isinstance(rgb_reference, dict) or not isinstance(depth_reference, dict):
            self._mark_waiting(sessions, "aligned RGB-D references unavailable")
            time.sleep(self.args.poll_interval)
            return

        frame_number = int(rgb_reference.get("frame_number", -1))
        if frame_number < 0:
            raise RuntimeError("RGB BufferRef does not contain a valid frame_number")
        now = time.monotonic()
        due_sessions = [
            session
            for session in sessions
            if frame_number > session.last_frame_number
            and now - session.last_update_monotonic >= 1.0 / session.max_update_hz
        ]
        if not due_sessions:
            self._expire_sessions(sessions, now)
            time.sleep(self.args.poll_interval)
            return

        rgb = self._read_rgb(rgb_reference)
        depth_m = self._read_depth_m(depth_reference)
        if depth_m.shape != rgb.shape[:2]:
            depth_m = cv2.resize(
                depth_m,
                (rgb.shape[1], rgb.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            )
        timestamp_us = self._reference_timestamp(rgb_reference)
        if timestamp_us <= 0:
            timestamp_us = int(bundle_observation.get("observed_at_us") or time.time_ns() // 1000)
        self.last_camera_frame_number = frame_number

        for session in due_sessions:
            self._process_session(
                session,
                rgb=rgb,
                depth_m=depth_m,
                frame_number=frame_number,
                observed_at_us=timestamp_us,
            )
        self._expire_sessions(sessions, now)
        time.sleep(self.args.poll_interval)

    def _process_session(
        self,
        session: PoseSession,
        *,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        frame_number: int,
        observed_at_us: int,
    ) -> None:
        try:
            model = self.registry.get(
                session.model_id, require_mesh=self.args.backend != "mock"
            )
            if not session.initialized:
                mask = self._load_initial_mask(session, rgb.shape[:2])
                if mask is None:
                    session.state = "WAITING_FOR_INPUTS"
                    session.last_error = "initial object mask unavailable"
                    self._best_effort_status(session, session.last_error)
                    session.last_frame_number = frame_number
                    return
                session.state = "INITIALIZING"
                quality = self._input_quality(depth_m, mask)
                if quality["mask_pixel_count"] < self.args.minimum_mask_pixels:
                    raise RuntimeError(
                        f"initial mask is too small: {quality['mask_pixel_count']} pixels"
                    )
                result = self.backend.initialize(
                    session.session_id,
                    model,
                    rgb,
                    depth_m,
                    self._require_camera_matrix(),
                    mask,
                )
                mode = "INITIAL_ESTIMATE"
                session.initialized = True
            else:
                mask = None
                quality = self._input_quality(depth_m, None)
                result = self.backend.track(
                    session.session_id,
                    rgb,
                    depth_m,
                    self._require_camera_matrix(),
                )
                mode = "TRACKED"

            camera_from_semantic = result.camera_from_mesh @ model.mesh_from_semantic
            session.last_frame_number = frame_number
            session.last_update_monotonic = time.monotonic()
            session.last_observed_at_us = observed_at_us
            session.last_latency_ms = result.latency_ms
            session.result_count += 1
            session.consecutive_failures = 0
            session.last_error = None
            session.state = "COMPLETED" if session.operation == "ESTIMATE" else "TRACKING"
            self.health = "HEALTHY"
            self.last_error = None
            self._publish_pose_result(
                session=session,
                model=model,
                result=result,
                camera_from_semantic=camera_from_semantic,
                frame_number=frame_number,
                observed_at_us=observed_at_us,
                mode=mode,
                quality=quality,
            )
            if session.state == "COMPLETED":
                self.backend.reset(session.session_id)
        except Exception as error:
            session.last_frame_number = frame_number
            session.last_update_monotonic = time.monotonic()
            session.consecutive_failures += 1
            session.last_error = str(error)
            session.state = (
                "FAILED"
                if session.consecutive_failures >= self.args.max_consecutive_failures
                else "DEGRADED"
            )
            if session.state == "FAILED":
                self.backend.reset(session.session_id)
            self.health = "DEGRADED"
            self.last_error = f"session {session.session_id}: {error}"
            self._best_effort_status(session, session.last_error)

    def _expire_sessions(self, sessions: list[PoseSession], now: float) -> None:
        for session in sessions:
            if now - session.created_monotonic <= session.max_duration_s:
                continue
            self.backend.reset(session.session_id)
            session.state = "EXPIRED"
            session.last_error = "session duration expired"
            self._best_effort_status(session, session.last_error)

    def _mark_waiting(self, sessions: list[PoseSession], message: str) -> None:
        now = time.monotonic()
        for session in sessions:
            session.state = "WAITING_FOR_INPUTS"
            session.last_error = message
        if now - self.last_status_publish_monotonic >= 1.0 and sessions:
            self.last_status_publish_monotonic = now
            self._best_effort_status(sessions[0], message)

    def _configure_calibration(self, observation: dict[str, Any]) -> None:
        data = observation.get("data") or {}
        rgb = data.get("rgb_intrinsic") or {}
        matrix = np.array(
            [
                [float(rgb.get("fx", 0.0)), 0.0, float(rgb.get("cx", 0.0))],
                [0.0, float(rgb.get("fy", 0.0)), float(rgb.get("cy", 0.0))],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
            raise RuntimeError("RGB camera intrinsics are invalid")
        self.camera_matrix = matrix
        self.camera_calibration_revision = observation.get("calibration_revision")
        coordinate_frame = observation.get("coordinate_frame")
        if coordinate_frame:
            self.camera_frame = str(coordinate_frame)

    def _require_camera_matrix(self) -> np.ndarray:
        if self.camera_matrix is None:
            raise RuntimeError("camera calibration is not configured")
        return self.camera_matrix

    def _load_initial_mask(
        self, session: PoseSession, image_shape: tuple[int, int]
    ) -> Optional[np.ndarray]:
        height, width = image_shape
        mask: Optional[np.ndarray] = None
        if session.mask_path is not None:
            mask = cv2.imread(str(session.mask_path), cv2.IMREAD_GRAYSCALE)
            if mask is None:
                raise FileNotFoundError(f"could not read mask image: {session.mask_path}")
        elif session.bounding_box is not None:
            mask = session.bounding_box.to_mask(height, width)
        else:
            observation = self._latest_optional(session.mask_stream)
            if observation is None:
                return None
            data = observation.get("data")
            if not isinstance(data, dict):
                return None
            target_id = data.get("target_id")
            model_id = data.get("model_id")
            requested_session = data.get("tracking_session_id") or data.get("session_id")
            if target_id is not None and str(target_id) != session.target_id:
                return None
            if model_id is not None and str(model_id) != session.model_id:
                return None
            if requested_session is not None and str(requested_session) != session.session_id:
                return None
            reference = self._extract_buffer_reference(data)
            if reference is None:
                return None
            mask = self._read_mask(reference)
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        return np.ascontiguousarray(mask > 0)

    @staticmethod
    def _extract_buffer_reference(data: dict[str, Any]) -> Optional[dict[str, Any]]:
        candidates = [data, data.get("mask"), data.get("mask_ref"), data.get("buffer_ref")]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("mapping_name"):
                return candidate
        return None

    @staticmethod
    def _input_quality(depth_m: np.ndarray, mask: Optional[np.ndarray]) -> dict[str, Any]:
        finite_depth = np.isfinite(depth_m) & (depth_m > 0.0)
        if mask is None:
            support = finite_depth
            mask_pixels = int(depth_m.size)
        else:
            support = finite_depth & (mask > 0)
            mask_pixels = int(np.count_nonzero(mask))
        valid_pixels = int(np.count_nonzero(support))
        return {
            "mask_pixel_count": mask_pixels,
            "valid_depth_pixel_count": valid_pixels,
            "valid_depth_ratio": float(valid_pixels / max(1, mask_pixels)),
            "image_valid_depth_ratio": float(np.count_nonzero(finite_depth) / max(1, depth_m.size)),
        }

    def _read_rgb(self, reference: dict[str, Any]) -> np.ndarray:
        payload = self._read_reference(reference)
        format_name = str(reference.get("format_name", "")).upper()
        width = int(reference["width"])
        height = int(reference["height"])
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise RuntimeError("OpenCV could not decode the RGB JPEG frame")
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
        raise RuntimeError(f"unsupported RGB format: {format_name or 'unknown'}")

    def _read_depth_m(self, reference: dict[str, Any]) -> np.ndarray:
        payload = self._read_reference(reference)
        format_name = str(reference.get("format_name", "")).upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format: {format_name or 'unknown'}")
        width = int(reference["width"])
        height = int(reference["height"])
        expected = width * height
        values = np.frombuffer(payload, dtype="<u2", count=expected)
        if values.size != expected:
            raise RuntimeError("aligned depth payload is shorter than declared dimensions")
        scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * (scale_mm / 1000.0)

    def _read_mask(self, reference: dict[str, Any]) -> np.ndarray:
        payload = self._read_reference(reference)
        format_name = str(reference.get("format_name", "")).upper()
        if format_name in {"PNG", "JPEG", "JPG", "MJPG", "MJPEG"}:
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if decoded is None:
                raise RuntimeError("OpenCV could not decode the mask image")
            return decoded
        width = int(reference["width"])
        height = int(reference["height"])
        expected = width * height
        if format_name in {"Y8", "GRAY8", "MONO8", "MASK8", "U8"}:
            values = np.frombuffer(payload, dtype=np.uint8, count=expected)
            if values.size != expected:
                raise RuntimeError("mask payload is shorter than declared dimensions")
            return values.reshape(height, width).copy()
        if format_name in {"Y16", "GRAY16", "MASK16", "U16"}:
            values = np.frombuffer(payload, dtype="<u2", count=expected)
            if values.size != expected:
                raise RuntimeError("mask payload is shorter than declared dimensions")
            return (values.reshape(height, width) > 0).astype(np.uint8) * 255
        raise RuntimeError(f"unsupported mask format: {format_name or 'unknown'}")

    def _read_reference(self, reference: dict[str, Any]) -> bytes:
        mapping_name = str(reference.get("mapping_name") or "")
        if not mapping_name:
            raise RuntimeError("BufferRef does not include a mapping_name")
        reader = self.readers.get(mapping_name)
        if reader is None:
            try:
                from orbbec_femto_provider.shared_memory_access import CameraSharedMemory
            except ImportError as error:
                raise RuntimeError(
                    "orbbec-femto-provider is required to read Windows camera BufferRefs"
                ) from error
            reader = CameraSharedMemory(mapping_name).open()
            self.readers[mapping_name] = reader
        return reader.read_ref(reference)

    def _close_readers(self) -> None:
        for reader in self.readers.values():
            try:
                reader.close()
            except Exception:
                pass
        self.readers.clear()

    @staticmethod
    def _reference_timestamp(reference: dict[str, Any]) -> int:
        return int(
            reference.get("system_timestamp_us")
            or reference.get("global_timestamp_us")
            or reference.get("device_timestamp_us")
            or 0
        )

    def _publish_pose_result(
        self,
        *,
        session: PoseSession,
        model: ObjectModel,
        result: BackendResult,
        camera_from_semantic: np.ndarray,
        frame_number: int,
        observed_at_us: int,
        mode: str,
        quality: dict[str, Any],
    ) -> None:
        transform = transform_payload(camera_from_semantic)
        covariance = diagonal_covariance(
            (0.015, 0.015, 0.025) if mode == "INITIAL_ESTIMATE" else (0.025, 0.025, 0.04),
            (0.035, 0.035, 0.06) if mode == "INITIAL_ESTIMATE" else (0.06, 0.06, 0.1),
        )
        common = {
            "tracking_session_id": session.session_id,
            "target_id": session.target_id,
            "model_id": session.model_id,
            "model_revision": model.revision,
            "registry_revision": self.registry.revision,
            "object_role": model.role,
            "semantic_frame": model.semantic_frame,
            "source_frame_number": frame_number,
            "source_observed_at_us": observed_at_us,
            "parent_frame": session.parent_frame,
            "child_frame": session.child_frame,
            "mode": mode,
            "tracking_state": session.state,
            "score": result.score,
            "latency_ms": result.latency_ms,
            "quality": quality,
            "backend": self.args.backend,
            "backend_details": result.backend_details,
            "covariance_6x6": covariance,
            "covariance_order": ["tx", "ty", "tz", "rx", "ry", "rz"],
            "covariance_basis": "HEURISTIC_NOT_CALIBRATED",
            "related_skill_id": session.related_skill_id,
            **transform,
        }
        self.sequence += 1
        observations = [
            self._observation(
                stream="perception.object.pose",
                schema="physical_agent.object_pose_measurement",
                observed_at_us=observed_at_us,
                coordinate_frame=session.parent_frame,
                data=common,
                freshness_ms=self.args.pose_freshness_ms,
            ),
            self._observation(
                stream="transform.foundation_pose.object",
                schema="physical_agent.transform",
                observed_at_us=observed_at_us,
                coordinate_frame=session.parent_frame,
                data={
                    "parent_frame": session.parent_frame,
                    "child_frame": session.child_frame,
                    "translation_m": transform["translation_m"],
                    "rotation_xyzw": transform["quaternion_xyzw"],
                    "is_static": False,
                    "authority": f"{self.provider_id}:{self.instance_id}",
                    "session_epoch": session.session_id,
                    "calibration_revision": self.camera_calibration_revision,
                    "covariance_6x6": covariance,
                    "covariance_order": ["tx", "ty", "tz", "rx", "ry", "rz"],
                    "continuity": "MEASUREMENT",
                    "source": {
                        "model_id": session.model_id,
                        "object_role": model.role,
                        "semantic_frame": model.semantic_frame,
                        "source_frame_number": frame_number,
                        "mode": mode,
                    },
                },
                freshness_ms=self.args.pose_freshness_ms,
            ),
            self._status_observation(session, "pose result published", observed_at_us),
        ]
        response = self.http.post(
            f"{self.args.fabric_url}/v1/observations/batch",
            json={"observations": observations},
        )
        response.raise_for_status()

    def _best_effort_status(self, session: PoseSession, message: str) -> None:
        try:
            self._publish_session_status(session, message)
        except Exception as error:
            session.last_error = f"{message}; status publish failed: {error}"
            self.last_error = session.last_error

    def _publish_session_status(self, session: PoseSession, message: str) -> None:
        self.sequence += 1
        observed_at_us = session.last_observed_at_us or int(time.time_ns() // 1000)
        response = self.http.post(
            f"{self.args.fabric_url}/v1/observations",
            json=self._status_observation(session, message, observed_at_us),
        )
        if response.status_code != 404:
            response.raise_for_status()

    def _status_observation(
        self, session: PoseSession, message: str, observed_at_us: int
    ) -> dict[str, Any]:
        return self._observation(
            stream="perception.object_pose.status",
            schema="physical_agent.object_pose_status",
            observed_at_us=observed_at_us,
            coordinate_frame=session.parent_frame,
            data={
                **session.public_payload(),
                "message": message,
                "backend": self.args.backend,
                "camera_calibration_revision": self.camera_calibration_revision,
            },
            freshness_ms=2000,
        )

    def _observation(
        self,
        *,
        stream: str,
        schema: str,
        observed_at_us: int,
        coordinate_frame: Optional[str],
        data: Any,
        freshness_ms: Optional[int],
    ) -> dict[str, Any]:
        return {
            "schema": schema,
            "schema_version": 1,
            "stream": stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": max(0, int(observed_at_us)),
            "freshness_ms": freshness_ms,
            "coordinate_frame": coordinate_frame,
            "calibration_revision": self.camera_calibration_revision,
            "clock_domain": "camera_system_timestamp_preferred",
            "valid": True,
            "data": data,
        }

    def _latest_cached(
        self, stream: str, *, refresh_s: float
    ) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        cached = self.cached_inputs.get(stream)
        if cached is not None and now - cached[0] < max(0.0, refresh_s):
            return cached[1]
        value = self._latest_optional(stream)
        self.cached_inputs[stream] = (now, value)
        return value

    def _latest_optional(self, stream: str) -> Optional[dict[str, Any]]:
        encoded = quote(stream, safe="")
        response = self.http.get(f"{self.args.fabric_url}/v1/latest/{encoded}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else None

    def _heartbeat(self) -> None:
        try:
            response = self.http.post(
                f"{self.args.manager_url}/v1/providers/heartbeat",
                json=self._status_payload(),
            )
            response.raise_for_status()
            self.manager_error = None
        except Exception as error:
            self.manager_error = f"manager heartbeat failed: {error}"

    def _status_payload(self) -> dict[str, Any]:
        active = [session for session in self.sessions.values() if session.state in ACTIVE_STATES]
        terminal = [session for session in self.sessions.values() if session.state in TERMINAL_STATES]
        capability_ready = self.residency == "HOT" and self.ready
        return {
            "provider_id": self.provider_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "residency": self.residency,
            "health": self.health,
            "ready": capability_ready,
            "pid": os.getpid(),
            "details": {
                "provider_version": PROVIDER_VERSION,
                "backend": self.args.backend,
                "backend_diagnostics": self.backend.diagnostics(),
                "registry_revision": self.registry.revision,
                "model_count": len(self.registry.models),
                "active_session_count": len(active),
                "terminal_session_count": len(terminal),
                "sessions": [session.public_payload() for session in self.sessions.values()],
                "camera_frame": self.camera_frame,
                "camera_calibration_revision": self.camera_calibration_revision,
                "last_camera_frame_number": self.last_camera_frame_number,
                "last_error": self.last_error,
                "manager_error": self.manager_error,
                "capability_readiness": {
                    "perception.object_pose": capability_ready,
                    "perception.object_pose.estimate": capability_ready,
                    "perception.object_pose.track": capability_ready,
                    "perception.object_pose.bounding_box_init": capability_ready,
                    "perception.object_pose.relocalize": capability_ready,
                    "perception.object_pose.stop": capability_ready,
                    "perception.object_pose.model_registry": True,
                },
                "resource_profile": {
                    "basis": "ESTIMATED",
                    "ram_mb": 1200,
                    "vram_mb": 6000,
                    "cpu_cores_expected": 2.0,
                    "gpu_required": self.args.backend == "nvlabs",
                },
            },
        }


class ControlHandler(BaseHTTPRequestHandler):
    provider: FoundationPoseProvider

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._reply(200, self.provider._status_payload())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/control/hot":
                result = self.provider.start_hot()
            elif self.path == "/v1/control/warm":
                result = self.provider.enter_warm()
            elif self.path == "/v1/control/stop":
                result = self.provider.stop()
            elif self.path == "/v1/control/request":
                result = self.provider.handle_request(self._read_json())
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, result)
        except Exception as error:
            self.provider.health = "DEGRADED"
            self.provider.last_error = str(error)
            self._reply(500, {"error": str(error)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[FoundationPoseControl] {format % args}", flush=True)

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--control-port", type=int, default=7103)
    parser.add_argument("--backend", choices=("nvlabs", "mock"), default="nvlabs")
    parser.add_argument("--foundationpose-root")
    parser.add_argument(
        "--model-registry",
        default=str(PROVIDER_ROOT / "models" / "models.example.json"),
    )
    parser.add_argument("--poll-interval", type=float, default=0.01)
    parser.add_argument("--default-update-hz", type=float, default=3.0)
    parser.add_argument("--default-track-duration-s", type=float, default=30.0)
    parser.add_argument("--pose-freshness-ms", type=int, default=750)
    parser.add_argument("--minimum-mask-pixels", type=int, default=256)
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    parser.add_argument("--estimate-iterations", type=int, default=5)
    parser.add_argument("--track-iterations", type=int, default=2)
    parser.add_argument("--prepared-model-cache-size", type=int, default=4)
    parser.add_argument("--debug-level", type=int, default=0)
    parser.add_argument(
        "--debug-dir", default=str(PROVIDER_ROOT / "debug" / "foundation_pose")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = FoundationPoseProvider(args)
    ControlHandler.provider = provider
    server = ThreadingHTTPServer(("127.0.0.1", args.control_port), ControlHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        provider.stop()

    if os.name != "nt":
        signal.signal(signal.SIGTERM, request_stop)
    try:
        return provider.run()
    except KeyboardInterrupt:
        provider.stop()
        return 130
    except Exception as error:
        provider.health = "UNHEALTHY"
        provider.last_error = str(error)
        print(f"[FoundationPoseProvider] fatal: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        provider._close_readers()
        provider.backend.close()
        provider.http.close()


if __name__ == "__main__":
    raise SystemExit(main())
