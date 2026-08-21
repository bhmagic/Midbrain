"""HOT SAM2 tracker for user-described arm-scene semantics."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROVIDER_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROVIDER_ROOT.parents[1]
WEB_ROOT = PROVIDER_ROOT / "web"
LOCAL_PYTHON = PROVIDER_ROOT / "python"
CAMERA_PYTHON = WORKSPACE_ROOT / "providers" / "orbbec_femto_bolt" / "python"
for entry in (LOCAL_PYTHON, CAMERA_PYTHON):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import httpx
from sam2_scene_tracker.annotator import build_scene_annotator
from sam2_scene_tracker.clients import FabricClient
from sam2_scene_tracker.engine import Sam2SceneTrackerEngine
from sam2_scene_tracker.fusion import PersistentSemanticVoxelMap
from sam2_scene_tracker.rgbd import RgbdCapture
from sam2_scene_tracker.sam_backend import Sam2ImageTracker
from sam2_scene_tracker.one_shot import segment_workspace_image


PROVIDER_ID = "perception.sam2_scene_tracker"
PROVIDER_VERSION = "0.3.2"


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("SAM2 tracker configuration must be an object")
    value = _expand(value)
    if not str(value.get("policy_stream") or "").strip():
        raise ValueError("SAM2 tracker requires a Fabric policy stream")
    if "bootstrap_policy" in value:
        raise ValueError(
            "SAM2 tracker bootstrap_policy is not supported; publish explicit "
            "user/upstream descriptions to the Fabric policy stream"
        )
    for name, default in (
        ("vlm_motion_refresh_interval_s", 20.0),
        ("vlm_stationary_refresh_interval_s", 40.0),
    ):
        refresh = float(value.get(name, default))
        if not 2.0 <= refresh <= 60.0:
            raise ValueError(f"{name} must be in [2, 60]")
    tracking_rate_hz = float(value.get("tracking_rate_hz", 1.0))
    if not 1.0 <= tracking_rate_hz <= 4.0:
        raise ValueError("tracking_rate_hz must be in [1, 4]")
    value["tracking_rate_hz"] = tracking_rate_hz
    angular_direction_count = int(value.get("angular_direction_count", 4096))
    if not 128 <= angular_direction_count <= 20_000:
        raise ValueError("angular_direction_count must be in [128, 20000]")
    angular_radius_scale = float(value.get("angular_radius_scale", 1.5))
    if not 1.0 <= angular_radius_scale <= 3.0:
        raise ValueError("angular_radius_scale must be in [1, 3]")
    angular_minimum_radius_m = float(
        value.get("angular_minimum_radius_m", 0.005)
    )
    if not 0.005 <= angular_minimum_radius_m <= 0.1:
        raise ValueError("angular_minimum_radius_m must be in [0.005, 0.1]")
    angular_radial_padding_m = float(
        value.get("angular_radial_padding_m", 0.003)
    )
    if not 0.0 <= angular_radial_padding_m <= 0.05:
        raise ValueError("angular_radial_padding_m must be in [0, 0.05]")
    angular_maximum_range_m = float(
        value.get("angular_maximum_range_m", 1.2)
    )
    if not 0.1 <= angular_maximum_range_m <= 1.2:
        raise ValueError("angular_maximum_range_m must be in [0.1, 1.2]")
    maximum_assertions = int(value.get("maximum_assertions", 20_000))
    if not angular_direction_count <= maximum_assertions <= 20_000:
        raise ValueError(
            "maximum_assertions must be between angular_direction_count and 20000"
        )
    aabb_freshness_ms = int(value.get("aabb_freshness_ms", 5000))
    if not 1000 <= aabb_freshness_ms <= 60_000:
        raise ValueError("aabb_freshness_ms must be in [1000, 60000]")
    padding = float(value.get("prompt_box_padding_fraction", 0.03))
    if not 0.0 <= padding <= 0.25:
        raise ValueError("prompt_box_padding_fraction must be in [0, 0.25]")
    connectivity = float(value.get("semantic_depth_connectivity_m", 0.035))
    if not 0.001 <= connectivity <= 0.25:
        raise ValueError("semantic_depth_connectivity_m must be in [0.001, 0.25]")
    for name, default in (
        ("work_object_mask_erosion_m", 0.01),
        ("keep_out_mask_erosion_m", 0.02),
    ):
        erosion_m = float(value.get(name, default))
        if not 0.0 <= erosion_m <= 0.1:
            raise ValueError(f"{name} must be in [0, 0.1]")
        value[name] = erosion_m
    candidates = value.get("vlm_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("vlm_candidates must be a non-empty ordered list")
    return value


class Sam2SceneTrackerProvider:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config(Path(args.config).resolve())
        self.provider_id = PROVIDER_ID
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.residency = "WARM"
        self.health = "HEALTHY"
        self.ready = False
        self.last_error: str | None = None
        self.manager_error: str | None = None
        self.last_tick_at_us: int | None = None
        self.last_arm_motion_monotonic: float | None = None
        self.current_tracking_interval_s = 1.0 / float(
            self.config.get("tracking_rate_hz", 1.0)
        )
        self.lock = threading.RLock()
        self.iteration_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.http = httpx.Client(timeout=5.0)
        self.fabric = FabricClient(args.fabric_url)
        checkpoint = Path(str(self.config["sam2_checkpoint"])).resolve()
        self.engine = Sam2SceneTrackerEngine(
            fabric=self.fabric,
            capture=RgbdCapture(self.fabric),
            annotator=build_scene_annotator(self.config, os.environ),
            tracker=Sam2ImageTracker(checkpoint),
            semantic_map=PersistentSemanticVoxelMap(
                fusion_voxel_edge_m=float(
                    self.config.get("fusion_voxel_edge_m", 0.02)
                ),
                deduplication_radius_m=float(
                    self.config.get("deduplication_radius_m", 0.016)
                ),
                maximum_voxels_per_object=int(
                    self.config.get("maximum_voxels_per_object", 100_000)
                ),
            ),
            config=self.config,
            provider_id=self.provider_id,
            provider_instance_id=self.instance_id,
            boot_id=self.boot_id,
        )
        self.heartbeat_thread: threading.Thread | None = None

    def _arm_motion_active(self) -> bool:
        now = time.monotonic()
        try:
            observation = self.fabric.latest_optional(
                str(self.config.get("arm_state_stream") or "robot_arm.joint_state")
            )
        except Exception:
            observation = None
        data = observation.get("data") if isinstance(observation, dict) else None
        data = data if isinstance(data, dict) else {}
        velocities = data.get("velocities_rad_s")
        threshold = float(
            self.config.get("arm_motion_velocity_threshold_rad_s", 0.03)
        )
        moving = False
        if isinstance(velocities, list):
            try:
                moving = max(abs(float(value)) for value in velocities[:6]) >= threshold
            except (TypeError, ValueError):
                moving = False
        if moving:
            self.last_arm_motion_monotonic = now
        hold_s = float(self.config.get("arm_motion_hold_s", 2.0))
        last = self.last_arm_motion_monotonic
        return last is not None and now - last <= hold_s

    def _recommended_tracking_interval(self) -> float:
        return 1.0 / float(self.config.get("tracking_rate_hz", 1.0))

    def register(self) -> None:
        response = self.http.post(
            f"{self.args.manager_url}/v1/providers/register",
            json=self.status_payload(),
        )
        response.raise_for_status()

    def heartbeat(self) -> None:
        try:
            response = self.http.post(
                f"{self.args.manager_url}/v1/providers/heartbeat",
                json=self.status_payload(),
            )
            response.raise_for_status()
            self.manager_error = None
        except Exception as error:
            self.manager_error = f"manager heartbeat failed: {error}"

    def _heartbeat_loop(self) -> None:
        while not self.shutdown_event.wait(1.0):
            self.heartbeat()

    def start_hot(self) -> dict[str, Any]:
        with self.lock:
            already = self.residency == "HOT"
            self.residency = "HOT"
            self.health = "HEALTHY"
            self.last_error = None
        return {"status": "already_hot" if already else "hot"}

    def enter_warm(self) -> dict[str, Any]:
        with self.lock:
            self.residency = "WARM"
            self.ready = False
            self.health = "HEALTHY"
        with self.iteration_lock:
            self.engine.tracker.close()
        return {"status": "warm", "tracking_active": False}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self.residency = "STOPPING"
            self.ready = False
        self.shutdown_event.set()
        return {"status": "stopping"}

    def tick_once(self) -> dict[str, Any] | None:
        self.engine.set_arm_motion_active(self._arm_motion_active())
        with self.iteration_lock:
            observation = self.engine.tick()
        diagnostics = self.engine.last_diagnostics
        coverage = diagnostics.get("coverage")
        coverage = coverage if isinstance(coverage, dict) else {}
        with self.lock:
            self.last_tick_at_us = time.time_ns() // 1000
            self.ready = bool(coverage.get("ready"))
            self.health = "HEALTHY" if self.ready else "DEGRADED"
            self.last_error = None if self.ready else str(
                diagnostics.get("status") or "semantic coverage is not ready"
            )
        return observation

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()
        if action == "status":
            return self.status_payload()
        if action == "tick_once":
            if self.residency != "HOT":
                raise RuntimeError("tick_once requires HOT residency")
            return {
                "status": "completed",
                "observation": self.tick_once(),
                "diagnostics": self.engine.last_diagnostics,
            }
        if action == "segment_image":
            if self.residency != "HOT":
                raise RuntimeError("segment_image requires HOT residency")
            return self.segment_image(request.get("payload"))
        raise ValueError(f"unsupported SAM2 tracker action {action or 'empty'!r}")

    def segment_image(self, payload: Any) -> dict[str, Any]:
        return segment_workspace_image(
            payload=payload,
            tracker=self.engine.tracker,
            tracker_lock=self.iteration_lock,
            workspace_root=WORKSPACE_ROOT,
            artifact_root=(PROVIDER_ROOT / "run" / "one_shot_segmentation"),
            provider_id=self.provider_id,
            provider_instance_id=self.instance_id,
            boot_id=self.boot_id,
        )

    def status_payload(self) -> dict[str, Any]:
        diagnostics = self.engine.last_diagnostics
        return {
            "provider_id": self.provider_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "residency": self.residency,
            "health": self.health,
            "ready": self.ready,
            "pid": os.getpid(),
            "details": {
                "provider_version": PROVIDER_VERSION,
                "architecture_role": "HOT_USER_DESCRIBED_SAM2_SCENE_TRACKER",
                "last_error": self.last_error,
                "manager_error": self.manager_error,
                "last_tick_at_us": self.last_tick_at_us,
                "current_tracking_interval_s": self.current_tracking_interval_s,
                "current_tracking_rate_hz": 1.0 / self.current_tracking_interval_s,
                "diagnostics": diagnostics,
                "capability_readiness": {
                    "perception.scene.sam2.track": self.residency == "HOT",
                    "perception.image.sam2.segment": self.residency == "HOT",
                    "perception.scene.semantic_obstacles": self.ready,
                },
                "resource_profile": {
                    "basis": "ESTIMATED",
                    "cpu_cores_expected": 2.0,
                    "ram_mb": 800,
                    "vram_mb_measured_allocated": 476,
                    "vram_mb_measured_reserved": 1100,
                    "hot_advantage": (
                        "one loaded SAM2 model, configurable fixed-rate mask tracking, "
                        "occlusion-retained semantic voxel fusion, and slow "
                        "asynchronous VLM label refresh"
                    ),
                },
            },
        }

    def run(self) -> int:
        self.register()
        self.start_hot()
        self.heartbeat()
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="sam2-scene-manager-heartbeat",
        )
        self.heartbeat_thread.start()
        while not self.shutdown_event.is_set():
            started = time.monotonic()
            if self.residency != "HOT":
                self.shutdown_event.wait(0.1)
                continue
            try:
                self.tick_once()
            except Exception as error:
                with self.lock:
                    self.ready = False
                    self.health = "DEGRADED"
                    self.last_error = str(error)
            self.current_tracking_interval_s = self._recommended_tracking_interval()
            elapsed = time.monotonic() - started
            self.shutdown_event.wait(
                max(0.0, self.current_tracking_interval_s - elapsed)
            )
        return 0

    def close(self) -> None:
        self.shutdown_event.set()
        self.engine.close()
        self.fabric.close()
        self.http.close()


class ControlHandler(BaseHTTPRequestHandler):
    provider: Sam2SceneTrackerProvider

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/v1/status"}:
            self._json(200, self.provider.status_payload())
        elif path in {"/dev", "/dev/"}:
            self._file(WEB_ROOT / "developer.html", "text/html; charset=utf-8")
        elif path == "/dev/developer.css":
            self._file(WEB_ROOT / "developer.css", "text/css; charset=utf-8")
        elif path == "/dev/developer.js":
            self._file(
                WEB_ROOT / "developer.js",
                "text/javascript; charset=utf-8",
            )
        elif path == "/v1/diagnostics":
            self._json(200, self.provider.engine.last_diagnostics)
        elif path == "/v1/assertions":
            observation = self.provider.engine.last_observation
            if observation is None:
                self._json(404, {"error": "no tracked assertions are available"})
            else:
                self._json(200, observation)
        elif path in {
            "/v1/visualization.png",
            "/v1/visualization/composite.png",
        }:
            payload = self.provider.engine.latest_visualization_png
            if payload is None:
                self._json(404, {"error": "no SAM2 visualization is available"})
            else:
                self._bytes(200, payload, "image/png")
        elif path == "/v1/visualization/rgb.png":
            payload = self.provider.engine.latest_rgb_png
            if payload is None:
                self._json(404, {"error": "no RGB visualization is available"})
            else:
                self._bytes(200, payload, "image/png")
        elif path == "/v1/visualization/depth.png":
            payload = self.provider.engine.latest_depth_png
            if payload is None:
                self._json(404, {"error": "no depth visualization is available"})
            else:
                self._bytes(200, payload, "image/png")
        else:
            self._json(404, {"error": "not found"})

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
                self._json(404, {"error": "not found"})
                return
            self._json(200, result)
        except (ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error)})
        except Exception as error:
            self._json(500, {"error": str(error)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._bytes(status, payload, "application/json")

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json(404, {"error": "development asset is unavailable"})
            return
        self._bytes(200, path.read_bytes(), content_type)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[Sam2SceneTrackerControl] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--control-port", type=int, default=7105)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = Sam2SceneTrackerProvider(args)
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
        print(f"[Sam2SceneTrackerProvider] fatal: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
