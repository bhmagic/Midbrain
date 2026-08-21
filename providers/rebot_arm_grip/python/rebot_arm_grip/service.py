from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
import json
import threading
import time

from .authorization import AuthorizationError
from .controller import GripController, ThermalGateError


class GripService:
    def __init__(self, controller: GripController, config: dict[str, Any], platform: Any):
        self.controller = controller
        self.config = config
        self.platform = platform
        self.shutdown_event = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None
        self.platform_thread: threading.Thread | None = None
        self.manager_registered = False

    def start(self) -> None:
        self.controller.start()
        self.httpd = ThreadingHTTPServer(
            (str(self.config["listen_host"]), int(self.config["listen_port"])),
            self._handler(),
        )
        self.httpd.daemon_threads = True
        threading.Thread(
            target=self.httpd.serve_forever, name="grip-http", daemon=True
        ).start()
        try:
            self.platform.register(self._state())
            self.manager_registered = self.platform.manager_url is not None
        except Exception as exc:
            print(f"[grip-platform] Manager registration deferred: {exc}")
        self.platform_thread = threading.Thread(
            target=self._platform_loop, name="grip-platform", daemon=True
        )
        self.platform_thread.start()

    def shutdown(self) -> None:
        if self.shutdown_event.is_set():
            return
        self.controller.stop()
        self.shutdown_event.set()
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def _state(self) -> dict[str, Any]:
        state = self.controller.snapshot()
        state["manager_registered"] = self.manager_registered
        state["platform_errors"] = self.platform.errors()
        return state

    def _platform_loop(self) -> None:
        next_heartbeat = 0.0
        next_publish = 0.0
        while not self.shutdown_event.wait(0.02):
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    if self.manager_registered:
                        self.platform.heartbeat(self._state())
                    else:
                        self.platform.register(self._state())
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_heartbeat = now + 1.0
            if now >= next_publish:
                try:
                    self.platform.publish(self._state())
                except Exception:
                    pass
                next_publish = now + 0.1

    @staticmethod
    def _error(exc: Exception, code: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error": str(exc),
            "error_code": code,
            "physical_outcome_known": False,
            "affected_functions": ["robot_effector.grip"],
        }
        if isinstance(exc, ThermalGateError):
            result.update(
                {
                    "retry_recommendation": "WAIT_THEN_RETRY",
                    "retry_after_s": exc.retry_after_s,
                    "active_joint_temperatures_c": exc.temperatures,
                }
            )
        return result

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MidbrainGrip/0.2.6"

            def log_message(self, fmt, *args):
                return

            def _json(self, status: int, payload: Any):
                data = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(value, dict):
                    raise ValueError("request body must be an object")
                return value

            def do_GET(self):
                path = urlparse(self.path).path
                if path in {"/health", "/v1/state", "/v1/grip/state"}:
                    return self._json(200, service._state())
                if path == "/v1/capabilities":
                    return self._json(
                        200,
                        {
                            "provider_id": service.controller.provider_id,
                            "capabilities": [
                                "robot_effector.grip.position_effort_background.v1",
                                "robot_effector.grip.carrying_state.v1",
                                "robot_effector.grip.thermal_gate.v1",
                                "robot_effector.grip.mit_float_transition_50hz.v1",
                            ],
                        },
                    )
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                try:
                    path = urlparse(self.path).path
                    body = self._body()
                    if path == "/v1/grip/command":
                        assertion = str(
                            self.headers.get("X-Midbrain-Authorization")
                            or body.get("authorization")
                            or ""
                        )
                        command = body.get("command", body)
                        if not isinstance(command, dict):
                            raise ValueError("command must be an object")
                        return self._json(202, service.controller.submit(command, assertion))
                    if path == "/v1/control/hot":
                        service.controller.start()
                        return self._json(200, {"status": "hot"})
                    if path == "/v1/control/warm":
                        return self._json(200, service.controller.enter_warm())
                    if path in {"/v1/control/stop", "/v1/safe-terminate"}:
                        if service.controller.snapshot().get("carry") is not None:
                            return self._json(
                                409,
                                service._error(
                                    RuntimeError(
                                        "Grip Provider stop is rejected while carrying; release the object first"
                                    ),
                                    "CARRYING_STOP_REJECTED",
                                ),
                            )
                        threading.Thread(target=service.shutdown, daemon=True).start()
                        return self._json(202, {"status": "stopping"})
                    return self._json(404, {"error": "not found"})
                except AuthorizationError as exc:
                    return self._json(403, service._error(exc, "AUTHORIZATION_REJECTED"))
                except ThermalGateError as exc:
                    return self._json(409, service._error(exc, "THERMAL_GATE"))
                except PermissionError as exc:
                    return self._json(403, service._error(exc, "MOTION_NOT_AUTHORIZED"))
                except (KeyError, TypeError, ValueError) as exc:
                    return self._json(400, service._error(exc, "INVALID_REQUEST"))
                except RuntimeError as exc:
                    return self._json(409, service._error(exc, "STATE_CONFLICT"))
                except Exception as exc:
                    return self._json(500, service._error(exc, "GRIP_CONTROL_FAILED"))

        return Handler
