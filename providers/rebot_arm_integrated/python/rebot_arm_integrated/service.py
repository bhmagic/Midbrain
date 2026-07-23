from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import copy
import json
import mimetypes
import os
import subprocess
import threading
import time

from .controller import IntegratedController
from .platform import PlatformPublisher


class IntegratedService:
    def __init__(
        self,
        controller: IntegratedController,
        config: dict[str, Any],
        manager_url: str | None,
        fabric_url: str | None,
    ):
        self.controller = controller
        self.config = config
        self.shutdown_event = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None
        self.control_url = f"http://{config['listen_host']}:{config['listen_port']}"
        self.platform = PlatformPublisher(
            config["provider_id"], manager_url, fabric_url, self.control_url
        )
        self.web_root = Path(__file__).with_name("web")
        self.provider_root = Path(__file__).resolve().parents[2]
        self.publish_thread: threading.Thread | None = None
        self.manager_registered = False
        self.fabric_ready = False
        self.motion_inhibited = False
        self.motion_inhibit_owners: list[dict[str, Any]] = []
        self.request_results: dict[str, dict[str, Any]] = {}
        self.request_lock = threading.Lock()
        self.safe_termination_lock = threading.Lock()
        self.safe_termination: dict[str, Any] = {
            "state": "IDLE",
            "message": "",
            "started_at_monotonic": None,
        }
        self.fabric_input_last_key: tuple[str, str, int] | None = None
        self.fabric_input_status: dict[str, Any] = {
            "enabled": bool(config.get("fabric_input", {}).get("enabled", False)),
            "stream": str(config.get("fabric_input", {}).get("stream", "")),
            "schema": str(config.get("fabric_input", {}).get("schema", "")),
            "last_result": "WAITING",
            "last_error": None,
            "last_sequence": None,
            "last_provider_id": None,
            "last_age_ms": None,
            "accepted_count": 0,
            "stale_count": 0,
            "rejected_count": 0,
        }
        self.scene_input_last_key: tuple[str, str, int] | None = None
        self.scene_input_status: dict[str, Any] = {
            "enabled": bool(config.get("scene_input", {}).get("enabled", False)),
            "stream": str(config.get("scene_input", {}).get("stream", "")),
            "schema": str(config.get("scene_input", {}).get("schema", "")),
            "last_result": "WAITING",
            "last_error": None,
            "last_sequence": None,
            "last_age_ms": None,
            "accepted_count": 0,
            "stale_count": 0,
            "rejected_count": 0,
            "physical_motion_authorized": False,
        }

    def _sync_platform_state(self) -> None:
        self.controller.update_platform_status(
            self.manager_registered,
            self.fabric_ready,
            self.platform.errors(),
            motion_inhibited=self.motion_inhibited,
            motion_inhibit_owners=self.motion_inhibit_owners,
        )

    def start(self) -> None:
        self.shutdown_event.clear()
        self.controller.start(hot=True)
        try:
            self.httpd = ThreadingHTTPServer(
                (self.config["listen_host"], int(self.config["listen_port"])),
                self._handler(),
            )
            self.httpd.daemon_threads = True
            threading.Thread(
                target=self.httpd.serve_forever,
                name="staged-http",
                daemon=True,
            ).start()
        except Exception:
            self.controller.stop()
            raise

        try:
            self.platform.register(self.controller.snapshot())
            self.manager_registered = self.platform.manager_url is not None
        except Exception as exc:
            self.manager_registered = False
            print(f"[staged-platform] Manager registration deferred: {exc}")
        self._sync_platform_state()

        self.publish_thread = threading.Thread(
            target=self._publish_loop,
            name="staged-platform",
            daemon=True,
        )
        self.publish_thread.start()

    def _publish_loop(self) -> None:
        inhibit_poll_s = max(
            0.10,
            float(self.config["platform"].get("motion_inhibit_poll_ms", 200)) / 1000.0,
        )
        next_register = 0.0
        next_heartbeat = 0.0
        next_status = 0.0
        next_target = 0.0
        next_inhibit = 0.0
        next_fabric_input = 0.0
        next_scene_input = 0.0
        fabric_input_cfg = self.config.get("fabric_input", {})
        fabric_input_poll_s = max(0.02, float(fabric_input_cfg.get("poll_ms", 50)) / 1000.0)
        scene_input_cfg = self.config.get("scene_input", {})
        scene_input_poll_s = max(0.02, float(scene_input_cfg.get("poll_ms", 100)) / 1000.0)

        while not self.shutdown_event.wait(0.02):
            now = time.monotonic()
            state = self.controller.snapshot()

            if not self.manager_registered and now >= next_register:
                try:
                    self.platform.register(state)
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_register = now + 2.0

            if now >= next_heartbeat:
                try:
                    self.platform.heartbeat(state)
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_heartbeat = now + 1.0

            if now >= next_inhibit:
                try:
                    inhibit = self.platform.motion_inhibit()
                    self.motion_inhibited = bool(inhibit.get("inhibited", False))
                    owners = inhibit.get("owners", [])
                    self.motion_inhibit_owners = owners if isinstance(owners, list) else []
                except Exception:
                    self.manager_registered = False
                next_inhibit = now + inhibit_poll_s

            if bool(fabric_input_cfg.get("enabled", False)) and now >= next_fabric_input:
                self._consume_fabric_input()
                next_fabric_input = now + fabric_input_poll_s
            if bool(scene_input_cfg.get("enabled", False)) and now >= next_scene_input:
                self._consume_scene_input()
                next_scene_input = now + scene_input_poll_s

            if now >= next_status:
                try:
                    self.platform.publish(
                        "robot_arm.integrated.status",
                        "physical_agent.arm_integrated_mit_bringup_state",
                        state,
                    )
                    self.fabric_ready = self.platform.fabric_url is not None
                except Exception:
                    self.fabric_ready = False
                next_status = now + 0.2

            if now >= next_target:
                try:
                    target = {
                        "control_mode": state.get("control_mode"),
                        "control_state": state.get("control_state"),
                        "target": state.get("target"),
                        "joint_state": state.get("joint_state"),
                        "units": state.get("units"),
                    }
                    self.platform.publish(
                        "robot_arm.integrated.control_target",
                        "physical_agent.arm_control_target",
                        target,
                        freshness_ms=300,
                    )
                    self.fabric_ready = self.platform.fabric_url is not None
                except Exception:
                    self.fabric_ready = False
                next_target = now + 0.2

            self._sync_platform_state()

    def _consume_fabric_input(self) -> None:
        cfg = self.config.get("fabric_input", {})
        stream = str(cfg.get("stream", "")).strip()
        expected_schema = str(cfg.get("schema", "")).strip()
        try:
            observation = self.platform.latest(stream)
            if observation is None:
                self.fabric_input_status["last_result"] = "NO_OBSERVATION"
                self.fabric_input_status["last_error"] = None
                return
            if expected_schema and str(observation.get("schema", "")) != expected_schema:
                raise ValueError(
                    f"Fabric input schema {observation.get('schema')!r} does not match {expected_schema!r}"
                )
            if observation.get("valid") is False:
                self.fabric_input_status["last_result"] = "INVALID_IGNORED"
                self.fabric_input_status["last_error"] = None
                return

            now_us = time.time_ns() // 1000
            observed_at_us = int(observation.get("observed_at_us") or 0)
            age_ms = None if observed_at_us <= 0 else max(0.0, (now_us - observed_at_us) / 1000.0)
            configured_max_age_ms = float(cfg.get("max_age_ms", 650))
            observation_freshness = observation.get("freshness_ms")
            allowed_age_ms = configured_max_age_ms
            if observation_freshness is not None:
                allowed_age_ms = min(allowed_age_ms, float(observation_freshness))
            expires_at_us = int(observation.get("expires_at_us") or 0)
            if (age_ms is not None and age_ms > allowed_age_ms) or (expires_at_us > 0 and now_us > expires_at_us):
                self.fabric_input_status["last_result"] = "STALE_IGNORED"
                self.fabric_input_status["last_error"] = None
                self.fabric_input_status["last_age_ms"] = age_ms
                self.fabric_input_status["stale_count"] += 1
                return

            key = (
                str(observation.get("provider_instance_id") or observation.get("provider_id") or ""),
                str(observation.get("boot_id") or ""),
                int(observation.get("sequence") or 0),
            )
            if key == self.fabric_input_last_key:
                self.fabric_input_status["last_result"] = "DUPLICATE"
                self.fabric_input_status["last_age_ms"] = age_ms
                return
            data = observation.get("data")
            if not isinstance(data, dict):
                raise ValueError("Fabric arm command data must be an object")
            result = self.controller.stage_external_command(
                data,
                source=f"fabric:{stream}",
                metadata={
                    "schema": observation.get("schema"),
                    "provider_id": observation.get("provider_id"),
                    "provider_instance_id": observation.get("provider_instance_id"),
                    "boot_id": observation.get("boot_id"),
                    "sequence": observation.get("sequence"),
                    "observed_at_us": observed_at_us,
                    "related_skill_id": observation.get("related_skill_id"),
                },
            )
            self.fabric_input_last_key = key
            self.fabric_input_status["last_result"] = "ACCEPTED"
            self.fabric_input_status["last_error"] = None
            self.fabric_input_status["last_sequence"] = key[2]
            self.fabric_input_status["last_provider_id"] = observation.get("provider_id")
            self.fabric_input_status["last_age_ms"] = age_ms
            self.fabric_input_status["accepted_count"] += 1
            self.fabric_input_status["physical_motion_authorized"] = bool(
                result.get("physical_motion_authorized", False)
            )
            self.platform.fabric_consume_error = None
        except Exception as exc:
            self.fabric_input_status["last_result"] = "REJECTED"
            self.fabric_input_status["last_error"] = str(exc)
            self.fabric_input_status["rejected_count"] += 1
            self.platform.fabric_consume_error = str(exc)

    def _consume_scene_input(self) -> None:
        cfg = self.config.get("scene_input", {})
        stream = str(cfg.get("stream", "")).strip()
        expected_schema = str(cfg.get("schema", "")).strip()
        try:
            observation = self.platform.latest(stream)
            if observation is None:
                self.scene_input_status["last_result"] = "NO_OBSERVATION"
                self.scene_input_status["last_error"] = None
                return
            if expected_schema and str(observation.get("schema", "")) != expected_schema:
                raise ValueError(f"Fabric scene schema {observation.get('schema')!r} does not match {expected_schema!r}")
            if observation.get("valid") is False:
                self.scene_input_status["last_result"] = "INVALID_IGNORED"
                return
            now_us = time.time_ns() // 1000
            observed_at_us = int(observation.get("observed_at_us") or 0)
            age_ms = None if observed_at_us <= 0 else max(0.0, (now_us - observed_at_us) / 1000.0)
            allowed_age_ms = float(cfg.get("max_age_ms", 1000))
            if observation.get("freshness_ms") is not None:
                allowed_age_ms = min(allowed_age_ms, float(observation["freshness_ms"]))
            expires_at_us = int(observation.get("expires_at_us") or 0)
            if (age_ms is not None and age_ms > allowed_age_ms) or (expires_at_us > 0 and now_us > expires_at_us):
                self.scene_input_status["last_result"] = "STALE_IGNORED"
                self.scene_input_status["last_age_ms"] = age_ms
                self.scene_input_status["stale_count"] += 1
                return
            key = (
                str(observation.get("provider_instance_id") or observation.get("provider_id") or ""),
                str(observation.get("boot_id") or ""),
                int(observation.get("sequence") or 0),
            )
            if key == self.scene_input_last_key:
                self.scene_input_status["last_result"] = "DUPLICATE"
                self.scene_input_status["last_age_ms"] = age_ms
                return
            data = observation.get("data")
            if not isinstance(data, dict):
                raise ValueError("Fabric semantic scene data must be an object")
            self.controller.stage_scene(data, source=f"fabric:{stream}")
            self.scene_input_last_key = key
            self.scene_input_status["last_result"] = "ACCEPTED"
            self.scene_input_status["last_error"] = None
            self.scene_input_status["last_sequence"] = key[2]
            self.scene_input_status["last_age_ms"] = age_ms
            self.scene_input_status["accepted_count"] += 1
        except Exception as exc:
            self.scene_input_status["last_result"] = "REJECTED"
            self.scene_input_status["last_error"] = str(exc)
            self.scene_input_status["rejected_count"] += 1
            self.platform.fabric_consume_error = str(exc)

    def _state_payload(self) -> dict[str, Any]:
        with self.safe_termination_lock:
            termination = copy.deepcopy(self.safe_termination)
        return {
            **self.controller.snapshot(),
            "safe_termination": termination,
            "fabric_input": copy.deepcopy(self.fabric_input_status),
            "scene_input": copy.deepcopy(self.scene_input_status),
        }

    def start_safe_termination(self) -> dict[str, Any]:
        with self.safe_termination_lock:
            if self.safe_termination["state"] not in {"IDLE", "FAILED"}:
                return copy.deepcopy(self.safe_termination)
            self.safe_termination = {
                "state": "STARTING",
                "message": "Safe termination requested",
                "started_at_monotonic": time.monotonic(),
            }

        if os.name == "nt":
            project_root = self.provider_root.parents[1].resolve()
            helper = self.provider_root / "scripts" / "safe_terminate_detached.ps1"
            if not helper.exists():
                self._set_safe_termination("FAILED", f"Safe termination helper is missing: {helper}")
                return {"status": "failed", "safe_termination": copy.deepcopy(self.safe_termination)}
            log_path = self.provider_root / "runtime_logs" / "safe_terminate.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            launch_id = f"{os.getpid()}-{time.time_ns()}"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} GUI launch requested. "
                    f"launch_id={launch_id}\n"
                )
            powershell = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if not powershell.exists():
                self._set_safe_termination(
                    "FAILED", f"Windows PowerShell executable is missing: {powershell}"
                )
                return {
                    "status": "failed",
                    "safe_termination": copy.deepcopy(self.safe_termination),
                }
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            process = subprocess.Popen(
                [
                    str(powershell),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(helper),
                    "-ProjectRoot",
                    str(project_root),
                    "-BasicUrl",
                    str(self.config["basic_controller_url"]),
                    "-IntegratedUrl",
                    self.control_url,
                    "-LaunchId",
                    launch_id,
                ],
                cwd=str(project_root),
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            acknowledgement = f"Authoritative safe termination started. launch_id={launch_id}"
            acknowledged = False
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    if acknowledgement in log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ):
                        acknowledged = True
                        break
                except OSError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            with self.safe_termination_lock:
                self.safe_termination["state"] = (
                    "RUNNING" if acknowledged else "LAUNCH_UNCONFIRMED"
                )
                self.safe_termination["message"] = (
                    "Authoritative shutdown helper acknowledged; safe-home is running"
                    if acknowledged
                    else (
                        "Shutdown helper did not acknowledge startup; use the official "
                        "terminal command and inspect the log"
                    )
                )
                self.safe_termination["log_path"] = str(log_path)
                self.safe_termination["launch_id"] = launch_id
                self.safe_termination["process_id"] = process.pid
            return {
                "status": "accepted" if acknowledged else "unconfirmed",
                "safe_termination": copy.deepcopy(self.safe_termination),
            }

        thread = threading.Thread(target=self._safe_termination_worker, name="safe-termination", daemon=True)
        thread.start()
        return {"status": "accepted", "safe_termination": copy.deepcopy(self.safe_termination)}

    def _set_safe_termination(self, state: str, message: str) -> None:
        with self.safe_termination_lock:
            self.safe_termination["state"] = state
            self.safe_termination["message"] = message

    def _safe_termination_worker(self) -> None:
        try:
            self._set_safe_termination("RELEASING_LEASE", "Floating and releasing Integrated control lease")
            self.controller.enter_warm()
            if self.controller.basic.lease_snapshot() is not None:
                raise RuntimeError("Integrated WARM did not release the Basic lease")

            self._set_safe_termination("SAFE_HOMING", "Basic Controller is executing safe-home")
            self.controller.basic.safe_home_stop()
            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                try:
                    self.controller.basic.health()
                except Exception:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "Basic is still running after the safe-home window; core was NOT stopped so gravity support can remain active"
                )

            self._set_safe_termination("ARM_SAFE", "Basic safe-home completed; stopping Midbrain workspace")
            project_root = Path.cwd()
            stop_script = project_root / "platform_core" / "scripts" / "stop_workspace.ps1"
            if os.name == "nt" and stop_script.exists():
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
                    cwd=str(project_root),
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._set_safe_termination("STOPPING_CORE", "Safe-home complete; workspace stop launched")
            else:
                self._set_safe_termination("ARM_SAFE", "Safe-home complete; stop Midbrain core manually")
                timer = threading.Timer(0.5, self.shutdown)
                timer.daemon = True
                timer.start()
        except Exception as exc:
            self._set_safe_termination("FAILED", str(exc))

    def shutdown(self) -> None:
        if self.shutdown_event.is_set():
            return
        self.controller.stop()
        self.shutdown_event.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def _manager_request(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action", "")).strip()
        request_id = str(body.get("request_id") or "").strip()
        if not action:
            raise ValueError("action is required")
        if request_id:
            with self.request_lock:
                cached = self.request_results.get(request_id)
                if cached is not None:
                    return copy.deepcopy(cached)

        if action in {"gravity_float", "disengage"}:
            result = self.controller.request_float()
        elif action == "warm":
            result = self.controller.enter_warm()
        elif action == "hot":
            result = self.controller.enter_hot()
        else:
            raise ValueError(
                f"unsupported action {action}; Manager requests cannot authorize motion"
            )

        result = {**result, "request_id": request_id or None, "idempotent": bool(request_id)}
        if request_id:
            with self.request_lock:
                self.request_results[request_id] = copy.deepcopy(result)
                while len(self.request_results) > 128:
                    self.request_results.pop(next(iter(self.request_results)))
        return result

    def capability_catalog(self) -> dict[str, Any]:
        state = self.controller.snapshot()
        profiles = state.get("capability_profiles", {})
        readiness = state.get("capability_readiness", {})
        capabilities = []
        for capability, available in readiness.items():
            capabilities.append(
                {
                    "capability": capability,
                    "available": bool(available),
                    **copy.deepcopy(profiles.get(capability, {})),
                }
            )
        return {
            "schema": "physical_agent.provider_capability_catalog",
            "schema_version": 1,
            "provider_id": self.config["provider_id"],
            "manager_catalog_source": "heartbeat.details.capability_readiness",
            "capabilities": capabilities,
            "upstream_operations": {
                "state": {"method": "GET", "path": "/v1/state"},
                "engage": {
                    "method": "POST",
                    "path": "/v1/engage",
                    "caller_policy": "OPERATOR_OR_OPERATOR_SUPERVISED_SKILL",
                },
                "teleop_input": {
                    "method": "POST",
                    "path": "/v1/teleop",
                    "caller_policy": "OPERATOR_OR_OPERATOR_SUPERVISED_SKILL",
                },
                "settings": {"method": "POST", "path": "/v1/settings"},
                "gripper_settings": {"method": "POST", "path": "/v1/gripper/settings"},
                "gripper_action": {"method": "POST", "path": "/v1/gripper"},
                "nonphysical_preview": {"method": "POST", "path": "/v1/preview"},
                "contact_baseline_capture": {"method": "POST", "path": "/v1/contact-baseline"},
                "semantic_scene_staging": {"method": "POST", "path": "/v1/scene"},
                "gravity_float": {"method": "POST", "path": "/v1/float"},
                "safe_terminate": {"method": "POST", "path": "/v1/safe-terminate"},
                "cartesian_target_staging": {
                    "transport": "FABRIC",
                    "stream": self.config.get("fabric_input", {}).get("stream"),
                    "schema": self.config.get("fabric_input", {}).get("schema"),
                },
            },
            "physical_execution_gate": {
                "authority": "OPERATOR",
                "required": ["GUI_ENGAGE", "XBOX_LB"],
                "upstream_motion_authority": False,
            },
            "non_discoverable_experiments": copy.deepcopy(
                state.get("non_discoverable_experiments", {})
            ),
        }

    @staticmethod
    def _error_payload(exc: Exception, code: str) -> dict[str, Any]:
        return {
            "error": str(exc),
            "error_code": code,
            "severity": "ERROR",
            "retry_recommendation": "RETRY_AFTER_STATE_CHANGE",
            "safety_impact": "MOTION_BLOCKED_OR_FLOAT_REQUESTED",
            "physical_outcome_known": False,
            "affected_functions": ["robot.motion.arm.integrated"],
        }

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArmIntegratedMIT/0.7.0"

            def log_message(self, fmt, *args):
                try:
                    status = int(args[1]) if len(args) > 1 else 200
                except (TypeError, ValueError):
                    status = 200
                if status >= 400:
                    print(f"[staged-http] {self.path} {fmt % args}")

            def _write_bytes(self, data: bytes) -> bool:
                try:
                    self.wfile.write(data)
                    return True
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return False

            def _json(self, status: int, payload: Any):
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                self._write_bytes(data)
                return None

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return {} if not length else json.loads(self.rfile.read(length))

            def _static(self, path: str):
                relative = "index.html" if path in {"/", ""} else path.lstrip("/")
                target = (service.web_root / relative).resolve()
                root = service.web_root.resolve()
                if root not in target.parents and target != root:
                    return self._json(404, {"error": "not found"})
                if not target.exists() or not target.is_file():
                    return self._json(404, {"error": "not found"})
                data = target.read_bytes()
                try:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                self._write_bytes(data)
                return None

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.end_headers()

            def do_GET(self):
                try:
                    if self.path == "/health":
                        return self._json(
                            200,
                            {
                                **service._state_payload(),
                                "platform_errors": service.platform.errors(),
                                "manager_registered": service.manager_registered,
                                "fabric_ready": service.fabric_ready,
                                "motion_inhibited": service.motion_inhibited,
                            },
                        )
                    if self.path == "/v1/state":
                        return self._json(200, service._state_payload())
                    if self.path == "/v1/config":
                        return self._json(200, service.config)
                    if self.path == "/v1/capabilities":
                        return self._json(200, service.capability_catalog())
                    if self.path == "/favicon.ico":
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    return self._static(self.path)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                except Exception as exc:
                    return self._json(500, service._error_payload(exc, "READ_FAILED"))

            def do_POST(self):
                try:
                    body = self._body()
                    if self.path == "/v1/control/hot":
                        return self._json(200, service.controller.enter_hot())
                    if self.path == "/v1/control/warm":
                        return self._json(200, service.controller.enter_warm())
                    if self.path == "/v1/control/stop":
                        timer = threading.Timer(0.15, service.shutdown)
                        timer.daemon = True
                        timer.start()
                        return self._json(202, {"status": "stopping_float_then_release"})
                    if self.path == "/v1/control/request":
                        return self._json(200, service._manager_request(body))
                    if self.path == "/v1/teleop":
                        service.controller.update_input(body)
                        return self._json(200, {"accepted": True})
                    if self.path == "/v1/engage":
                        return self._json(
                            200,
                            service.controller.set_engaged(bool(body.get("enabled", False))),
                        )
                    if self.path == "/v1/settings":
                        return self._json(200, service.controller.set_runtime_settings(body))
                    if self.path == "/v1/gripper/settings":
                        return self._json(200, service.controller.set_gripper_settings(body))
                    if self.path == "/v1/gripper":
                        return self._json(200, service.controller.request_gripper(str(body.get("action", ""))))
                    if self.path == "/v1/preview":
                        allowed = body.get("allowed_contact_object_ids", [])
                        if not isinstance(allowed, list):
                            raise ValueError("allowed_contact_object_ids must be an array")
                        return self._json(200, service.controller.preview_staged_target(
                            allowed_contact_object_ids={str(value) for value in allowed},
                            permit_pushable_contact=bool(body.get("permit_pushable_contact", False)),
                        ))
                    if self.path == "/v1/contact-baseline":
                        return self._json(200, service.controller.capture_contact_baseline())
                    if self.path == "/v1/scene":
                        return self._json(200, service.controller.stage_scene(body, source="operator-api"))
                    if self.path == "/v1/float":
                        return self._json(200, service.controller.request_float())
                    if self.path == "/v1/safe-terminate":
                        return self._json(202, service.start_safe_termination())
                    return self._json(404, {"error": "not found"})
                except PermissionError as exc:
                    return self._json(403, service._error_payload(exc, "MOTION_NOT_AUTHORIZED"))
                except (ValueError, RuntimeError) as exc:
                    return self._json(409, service._error_payload(exc, "STATE_CONFLICT"))
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                except Exception as exc:
                    return self._json(500, service._error_payload(exc, "INTERNAL_ERROR"))

        return Handler
