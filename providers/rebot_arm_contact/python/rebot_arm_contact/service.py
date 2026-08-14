from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
import json
import threading
import time

from .authorization import AuthorizationError
from .authority_state import evaluate_authority_coordination
from .controller import ContactController
from .platform import PlatformPublisher


class ContactService:
    def __init__(
        self,
        controller: ContactController,
        config: dict[str, Any],
        platform: PlatformPublisher,
    ):
        self.controller = controller
        self.config = config
        self.platform = platform
        self.shutdown_event = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None
        self.platform_thread: threading.Thread | None = None
        self.manager_registered = False
        self.motion_inhibit_owners: list[dict[str, Any]] = []
        self.authority_coordination: dict[str, Any] | None = None

    def start(self) -> None:
        try:
            self.controller.start()
            try:
                inhibit = self.platform.motion_inhibit()
                inhibited = bool(inhibit.get("inhibited", False))
                owners = inhibit.get("owners", [])
                self.motion_inhibit_owners = owners if isinstance(owners, list) else []
                self.controller.set_motion_inhibited(
                    inhibited,
                    "Manager motion inhibit was active during Contact Provider startup",
                )
            except Exception as exc:
                self.controller.set_motion_inhibited(
                    True,
                    f"Manager motion-inhibit state is unavailable: {exc}",
                )
            self.httpd = ThreadingHTTPServer(
                (str(self.config["listen_host"]), int(self.config["listen_port"])),
                self._handler(),
            )
            self.httpd.daemon_threads = True
            threading.Thread(
                target=self.httpd.serve_forever,
                name="contact-work-http",
                daemon=True,
            ).start()
            try:
                self.platform.register(self._platform_state())
                self.manager_registered = self.platform.manager_url is not None
            except Exception as exc:
                print(f"[contact-platform] Manager registration deferred: {exc}")
            self.platform_thread = threading.Thread(
                target=self._platform_loop,
                name="contact-work-platform",
                daemon=True,
            )
            self.platform_thread.start()
        except Exception:
            if self.httpd is not None:
                self.httpd.server_close()
                self.httpd = None
            self.controller.stop()
            raise

    def shutdown(self) -> None:
        if self.shutdown_event.is_set():
            return
        self.controller.stop()
        self.shutdown_event.set()
        if self.httpd is not None:
            threading.Thread(
                target=self.httpd.shutdown,
                name="contact-work-http-stop",
                daemon=True,
            ).start()

    def _platform_state(self) -> dict[str, Any]:
        state = self.controller.snapshot()
        state["manager_registered"] = self.manager_registered
        state["motion_inhibit_owners"] = list(self.motion_inhibit_owners)
        state["platform_errors"] = self.platform.errors()
        state["provider_instance_id"] = self.platform.instance_id
        state["provider_boot_id"] = self.platform.boot_id
        state["authority_coordination"] = self.authority_coordination
        return state

    def _poll_authority_coordination(self) -> None:
        state = self.controller.snapshot()
        resource_id = str(state.get("arm_resource_id") or "")
        manager_view = None
        available = False
        try:
            manager_view = self.platform.control_authority(resource_id)
            available = self.platform.manager_url is not None
        except Exception:
            available = False
        self.authority_coordination = evaluate_authority_coordination(
            resource_id=resource_id,
            manager_available=available,
            manager_view=manager_view,
            local_basic_lease=state.get("basic_lease"),
            upstream_authority=state.get("manager_authority_lineage"),
            local_writer_active=state.get("active_sequence") is not None,
            motion_inhibited=bool(state.get("motion_inhibited", False)),
        )

    def _platform_loop(self) -> None:
        next_heartbeat = 0.0
        next_publish = 0.0
        next_inhibit = 0.0
        next_authority = 0.0
        while not self.shutdown_event.wait(0.02):
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    if not self.manager_registered:
                        self.platform.register(self._platform_state())
                    else:
                        self.platform.heartbeat(self._platform_state())
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_heartbeat = now + 1.0
            if now >= next_inhibit:
                try:
                    inhibit = self.platform.motion_inhibit()
                    owners = inhibit.get("owners", [])
                    self.motion_inhibit_owners = owners if isinstance(owners, list) else []
                    self.controller.set_motion_inhibited(
                        bool(inhibit.get("inhibited", False)),
                        "Manager motion inhibit became active",
                    )
                except Exception as exc:
                    self.controller.set_motion_inhibited(
                        True,
                        f"Manager motion-inhibit state became unavailable: {exc}",
                    )
                next_inhibit = now + 0.2
            if now >= next_authority:
                self._poll_authority_coordination()
                next_authority = now + 0.5
            if now >= next_publish:
                try:
                    self.platform.publish(self._platform_state())
                except Exception:
                    pass
                next_publish = now + 0.1

    @staticmethod
    def _error(exc: Exception, code: str) -> dict[str, Any]:
        return {
            "error": str(exc),
            "error_code": code,
            "severity": "ERROR",
            "retry_recommendation": "RETRY_AFTER_STATE_CHANGE",
            "safety_impact": "MOTION_BLOCKED_OR_FLOAT_REQUESTED",
            "physical_outcome_known": False,
            "affected_functions": ["robot_arm.motion.contact"],
        }

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MidbrainContactWork/0.1.0"

            def log_message(self, fmt, *args):
                try:
                    status = int(args[1]) if len(args) > 1 else 200
                except (TypeError, ValueError):
                    status = 200
                if status >= 400:
                    print(f"[contact-http] {self.path} {fmt % args}")

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
                if length <= 0:
                    return {}
                decoded = json.loads(self.rfile.read(length))
                if not isinstance(decoded, dict):
                    raise ValueError("request body must be a JSON object")
                return decoded

            def do_GET(self):
                try:
                    path = urlparse(self.path).path
                    if path in {"/health", "/v1/contact/state", "/v1/state"}:
                        return self._json(200, service._platform_state())
                    if path == "/v1/capabilities":
                        return self._json(
                            200,
                            {
                                "provider_id": service.controller.provider_id,
                                "capabilities": [
                                    {
                                        "capability": "robot_arm.motion.contact.position_effort_limited.v1",
                                        "ready": service.controller.snapshot()["ready"],
                                        "role": "CONTACT",
                                    }
                                ],
                            },
                        )
                    return self._json(404, {"error": "not found"})
                except Exception as exc:
                    return self._json(500, service._error(exc, "READ_FAILED"))

            def do_POST(self):
                try:
                    body = self._body()
                    path = urlparse(self.path).path
                    if path == "/v1/contact/session":
                        assertion = str(
                            self.headers.get("X-Midbrain-Authorization")
                            or body.get("authorization")
                            or ""
                        )
                        return self._json(
                            201,
                            service.controller.begin_session(body["plan"], assertion),
                        )
                    if path == "/v1/control/hot":
                        return self._json(200, service.controller.enter_hot())
                    if path == "/v1/control/warm":
                        return self._json(200, service.controller.enter_warm())
                    if path == "/v1/contact/move":
                        return self._json(
                            202,
                            service.controller.move(
                                str(body["session_id"]), int(body["sequence"])
                            ),
                        )
                    if path in {"/v1/contact/relax", "/v1/float"}:
                        return self._json(
                            200,
                            service.controller.relax(
                                str(body.get("session_id") or ""),
                                str(body.get("reason") or "Contact Skill requested relax"),
                            ),
                        )
                    if path == "/v1/control/request":
                        action = str(body.get("action") or "").upper()
                        if action == "HOT":
                            return self._json(200, service.controller.enter_hot())
                        if action == "WARM":
                            return self._json(200, service.controller.enter_warm())
                        if action == "STATUS":
                            return self._json(200, service._platform_state())
                        if action in {"RELAX", "SAFE_RELINQUISH"}:
                            session = service.controller.snapshot().get("session_id")
                            if not session:
                                return self._json(200, {"disposition": "ALREADY_RELAXED"})
                            return self._json(
                                200,
                                service.controller.relax(
                                    str(session), "Manager requested safe relinquish"
                                ),
                            )
                        raise ValueError("unsupported Contact Provider control action")
                    if path in {"/v1/control/stop", "/v1/safe-terminate"}:
                        threading.Thread(
                            target=service.shutdown,
                            name="contact-work-stop-request",
                            daemon=True,
                        ).start()
                        return self._json(202, {"status": "stopping"})
                    return self._json(404, {"error": "not found"})
                except AuthorizationError as exc:
                    return self._json(403, service._error(exc, "AUTHORIZATION_REJECTED"))
                except PermissionError as exc:
                    return self._json(403, service._error(exc, "MOTION_NOT_AUTHORIZED"))
                except (KeyError, TypeError, ValueError) as exc:
                    return self._json(400, service._error(exc, "INVALID_REQUEST"))
                except RuntimeError as exc:
                    return self._json(409, service._error(exc, "STATE_CONFLICT"))
                except Exception as exc:
                    return self._json(500, service._error(exc, "CONTACT_CONTROL_FAILED"))

        return Handler
