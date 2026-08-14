"""HOT arm semantic-scene compiler Resource Provider."""

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

LOCAL_PYTHON_ROOT = Path(__file__).resolve().parent / "python"
if str(LOCAL_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_PYTHON_ROOT))

import httpx

from arm_scene_compiler.service import (
    FabricClient,
    PointCloudReader,
    SceneCompilerEngine,
    bounded_failure_retry_delay_s,
)


PROVIDER_ID = "world_model.arm_scene_compiler"
PROVIDER_VERSION = "0.1.0"


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("scene compiler configuration must be an object")
    required = {
        "point_cloud_streams",
        "semantic_assertion_streams",
        "assembly_state_stream",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("scene compiler configuration is missing: " + ", ".join(missing))
    return value


class ArmSceneCompilerProvider:
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
        self.last_compile_at_us: int | None = None
        self.consecutive_compile_failures = 0
        self.current_failure_retry_s = 0.0
        self.lock = threading.RLock()
        self.iteration_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.http = httpx.Client(timeout=5.0)
        self.fabric = FabricClient(args.fabric_url)
        self.point_reader = PointCloudReader()
        self.engine = SceneCompilerEngine(
            fabric=self.fabric,
            point_reader=self.point_reader,
            config=self.config,
            provider_id=self.provider_id,
            provider_instance_id=self.instance_id,
            boot_id=self.boot_id,
        )

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

    def start_hot(self) -> dict[str, Any]:
        with self.lock:
            already_hot = self.residency == "HOT"
            self.residency = "HOT"
            self.health = "HEALTHY"
            self.ready = False
            self.last_error = None
        return {"status": "already_hot" if already_hot else "hot"}

    def enter_warm(self) -> dict[str, Any]:
        with self.iteration_lock:
            with self.lock:
                self.residency = "WARM"
                self.health = "HEALTHY"
                self.ready = False
                self.last_error = None
                self.point_reader.close()
        return {"status": "warm", "scene_publication_active": False}

    def stop(self) -> dict[str, Any]:
        self.shutdown_event.set()
        with self.lock:
            self.residency = "STOPPING"
            self.ready = False
        self.point_reader.close()
        return {"status": "stopping"}

    def compile_once(self, *, force: bool = False) -> dict[str, Any] | None:
        with self.iteration_lock:
            result = self.engine.compile_once(force=force)
        if result is None:
            return None
        depth_mode = str((result.get("data") or {}).get("production", {}).get("depth_mode"))
        with self.lock:
            self.last_compile_at_us = time.time_ns() // 1000
            self.ready = True
            self.health = (
                "DEGRADED" if depth_mode == "SEMANTIC_ONLY" else "HEALTHY"
            )
            self.last_error = (
                "Depth geometry is unavailable; publishing fresh explicit semantic "
                "assertions only. Unobserved obstacles remain unknown."
                if depth_mode == "SEMANTIC_ONLY"
                else None
            )
        return result

    def _last_scene_is_current(self) -> bool:
        observation = self.engine.last_observation
        if not isinstance(observation, dict):
            return False
        observed_at_us = int(observation.get("observed_at_us") or 0)
        freshness_ms = int(observation.get("freshness_ms") or 0)
        expires_at_us = int(observation.get("expires_at_us") or 0)
        now_us = time.time_ns() // 1000
        if observed_at_us <= 0 or freshness_ms <= 0:
            return False
        return now_us <= observed_at_us + freshness_ms * 1000 and (
            expires_at_us <= 0 or now_us <= expires_at_us
        )

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()
        if action == "status":
            return self.status_payload()
        if action == "compile_once":
            if self.residency != "HOT":
                raise RuntimeError("compile_once requires HOT residency")
            observation = self.compile_once(force=True)
            return {
                "status": "compiled",
                "observation": observation,
                "diagnostics": self.engine.last_diagnostics,
            }
        raise ValueError(f"unsupported scene compiler action: {action or 'empty'}")

    def status_payload(self) -> dict[str, Any]:
        capability_ready = self.ready and self.residency == "HOT"
        last_scene = self.engine.last_observation
        last_scene_data = (
            last_scene.get("data")
            if isinstance(last_scene, dict) and isinstance(last_scene.get("data"), dict)
            else {}
        )
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
                "architecture_role": "HOT_SINGLE_OWNER_SCENE_COMPILER",
                "last_error": self.last_error,
                "manager_error": self.manager_error,
                "last_compile_at_us": self.last_compile_at_us,
                "consecutive_compile_failures": (
                    self.consecutive_compile_failures
                ),
                "current_failure_retry_s": self.current_failure_retry_s,
                "last_scene_revision": last_scene_data.get("scene_revision"),
                "last_scene_sphere_count": len(last_scene_data.get("spheres") or []),
                "diagnostics": self.engine.last_diagnostics,
                "capability_readiness": {
                    "world_model.arm.semantic_scene": capability_ready,
                    "world_model.arm.scene.inspect": self.residency in {"HOT", "WARM"},
                },
                "resource_profile": {
                    "basis": "ESTIMATED",
                    "ram_mb": 220,
                    "vram_mb": "NOT_APPLICABLE",
                    "cpu_cores_expected": 1.5,
                    "hot_advantage": (
                        "continuous BufferRef ingestion, current robot self-filtering, "
                        "short TTL, and monotonic scene revisions"
                    ),
                },
            },
        }

    def run(self) -> int:
        self.register()
        self.start_hot()
        heartbeat_at = 0.0
        poll_interval = max(
            0.05,
            float(self.config.get("poll_interval_s", 0.25)),
        )
        failure_retry_initial = max(
            poll_interval,
            float(self.config.get("failure_retry_initial_s", 0.5)),
        )
        failure_retry_maximum = max(
            failure_retry_initial,
            float(self.config.get("failure_retry_maximum_s", 5.0)),
        )
        while not self.shutdown_event.is_set():
            now = time.monotonic()
            if now >= heartbeat_at:
                self.heartbeat()
                heartbeat_at = now + 1.0
            if self.residency != "HOT":
                time.sleep(0.1)
                continue
            try:
                result = self.compile_once()
                with self.lock:
                    self.consecutive_compile_failures = 0
                    self.current_failure_retry_s = 0.0
                if result is None:
                    self.shutdown_event.wait(poll_interval)
            except Exception as error:
                with self.lock:
                    # A recycled camera slot is expected with a finite ring
                    # buffer. Keep advertising the last still-current scene
                    # while the next loop refreshes the reference.
                    self.ready = self._last_scene_is_current()
                    self.health = "DEGRADED"
                    self.last_error = str(error)
                    self.consecutive_compile_failures += 1
                    retry_delay = bounded_failure_retry_delay_s(
                        self.consecutive_compile_failures,
                        initial_s=failure_retry_initial,
                        maximum_s=failure_retry_maximum,
                    )
                    self.current_failure_retry_s = retry_delay
                self.shutdown_event.wait(retry_delay)
        return 0

    def close(self) -> None:
        self.point_reader.close()
        self.fabric.close()
        self.http.close()


class ControlHandler(BaseHTTPRequestHandler):
    provider: ArmSceneCompilerProvider

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health" or path == "/v1/status":
            self._reply(200, self.provider.status_payload())
        elif path == "/v1/scene":
            observation = self.provider.engine.last_observation
            if observation is None:
                self._reply(404, {"error": "no compiled scene is available"})
            else:
                self._reply(200, observation)
        elif path == "/v1/diagnostics":
            self._reply(200, self.provider.engine.last_diagnostics)
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
        except (ValueError, RuntimeError) as error:
            self._reply(409, {"error": str(error)})
        except Exception as error:
            self._reply(500, {"error": str(error)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[ArmSceneCompilerControl] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--control-port", type=int, default=7104)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = ArmSceneCompilerProvider(args)
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
        print(f"[ArmSceneCompilerProvider] fatal: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
