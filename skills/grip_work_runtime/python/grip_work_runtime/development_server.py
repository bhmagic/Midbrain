from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib import error, request
import json

from .development import NumberedProfileStore
from .runtime import HttpStatusError


PrepareCallback = Callable[[dict[str, Any]], dict[str, Any]]


class StagedDevelopmentExecution(Protocol):
    def observation(self) -> dict[str, Any]: ...

    def prepare(self, prepared_plan: dict[str, Any]) -> dict[str, Any]: ...

    def execute_stage(
        self,
        session_id: str,
        stage_number: int,
        *,
        physical_acknowledged: bool,
    ) -> dict[str, Any]: ...

    def cancel(self, session_id: str) -> dict[str, Any]: ...

    def profiles_locked(self) -> bool: ...


def _provider_state(url: str) -> dict[str, Any]:
    try:
        with request.urlopen(url, timeout=0.8) as response:
            value = json.loads(response.read())
        return value if isinstance(value, dict) else {"available": False}
    except (OSError, ValueError, error.URLError) as exc:
        return {"available": False, "error": str(exc)}


def run_development_server(
    *,
    port: int,
    title: str,
    skill_kind: str,
    vector_store: NumberedProfileStore,
    motion_store: NumberedProfileStore,
    motion_fields: list[dict[str, Any]],
    default_inputs: dict[str, Any],
    prepare: PrepareCallback,
    grip_state_url: str = "http://127.0.0.1:8795/v1/grip/state",
    staged_execution: StagedDevelopmentExecution | None = None,
) -> None:
    html = (
        Path(__file__).with_name("grip_developer.html").read_bytes()
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "MidbrainGripSkillDevelopment/1"

        def _json(self, status: int, value: dict[str, Any]) -> None:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 256 * 1024:
                raise ValueError("request body must be a bounded JSON object")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _state(self) -> dict[str, Any]:
            execution = (
                staged_execution.observation()
                if staged_execution is not None
                else {"available": False, "session": None}
            )
            return {
                "service": "midbrain-grip-skill-development",
                "title": title,
                "skill_kind": skill_kind,
                "vector_profiles": vector_store.snapshot(),
                "motion_profiles": motion_store.snapshot(),
                "motion_fields": motion_fields,
                "default_inputs": default_inputs,
                "grip_provider": _provider_state(grip_state_url),
                "staged_execution": execution,
                "execution_policy": execution.get(
                    "execution_policy",
                    "This Skill-owned page edits profiles and freezes a nonphysical plan.",
                ),
            }

        def _assert_profiles_unlocked(self) -> None:
            if staged_execution is not None and staged_execution.profiles_locked():
                raise RuntimeError(
                    "profiles and task inputs are frozen until the active development session ends"
                )

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, HttpStatusError):
                detail = dict(exc.payload)
                detail.setdefault("error", str(exc))
                self._json(exc.status_code, detail)
                return
            if isinstance(exc, PermissionError):
                self._json(403, {"error": str(exc), "error_type": type(exc).__name__})
                return
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                self._json(400, {"error": str(exc), "error_type": type(exc).__name__})
                return
            if isinstance(exc, RuntimeError):
                self._json(409, {"error": str(exc), "error_type": type(exc).__name__})
                return
            self._json(500, {"error": str(exc), "error_type": type(exc).__name__})

        def do_GET(self) -> None:
            try:
                if self.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if self.path == "/api/development":
                    self._json(200, self._state())
                    return
                self._json(404, {"error": "not found"})
            except Exception as exc:
                self._error(exc)

        def do_POST(self) -> None:
            try:
                if self.path == "/api/vector-profiles":
                    self._assert_profiles_unlocked()
                    self._json(201, {"saved": vector_store.add(self._payload())})
                    return
                if self.path == "/api/motion-profiles":
                    self._assert_profiles_unlocked()
                    self._json(201, {"saved": motion_store.add(self._payload())})
                    return
                if self.path == "/api/prepare":
                    self._assert_profiles_unlocked()
                    prepared = prepare(self._payload())
                    result: dict[str, Any] = {"prepared": prepared}
                    if staged_execution is not None:
                        result["session"] = staged_execution.prepare(prepared)
                    self._json(200, result)
                    return
                segments = [segment for segment in self.path.split("/") if segment]
                if (
                    staged_execution is not None
                    and len(segments) == 5
                    and segments[:2] == ["api", "development"]
                    and segments[3] == "stages"
                ):
                    payload = self._payload()
                    self._json(
                        200,
                        {
                            "session": staged_execution.execute_stage(
                                segments[2],
                                int(segments[4]),
                                physical_acknowledged=(
                                    payload.get("physical_acknowledged") is True
                                ),
                            )
                        },
                    )
                    return
                if (
                    staged_execution is not None
                    and len(segments) == 4
                    and segments[:2] == ["api", "development"]
                    and segments[3] == "cancel"
                ):
                    self._json(
                        200,
                        {"session": staged_execution.cancel(segments[2])},
                    )
                    return
                if len(segments) == 4 and segments[:2] == ["api", "vector-profiles"] and segments[3:] == ["default"]:
                    self._assert_profiles_unlocked()
                    self._json(200, vector_store.set_default(int(segments[2])))
                    return
                if len(segments) == 4 and segments[:2] == ["api", "motion-profiles"] and segments[3:] == ["default"]:
                    self._assert_profiles_unlocked()
                    self._json(200, motion_store.set_default(int(segments[2])))
                    return
                self._json(404, {"error": "not found"})
            except Exception as exc:
                self._error(exc)

        def do_DELETE(self) -> None:
            try:
                segments = [segment for segment in self.path.split("/") if segment]
                if len(segments) == 3 and segments[:2] == ["api", "vector-profiles"]:
                    self._assert_profiles_unlocked()
                    self._json(200, vector_store.delete(int(segments[2])))
                    return
                if len(segments) == 3 and segments[:2] == ["api", "motion-profiles"]:
                    self._assert_profiles_unlocked()
                    self._json(200, motion_store.delete(int(segments[2])))
                    return
                self._json(404, {"error": "not found"})
            except Exception as exc:
                self._error(exc)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    print(f"{title}: http://127.0.0.1:{port}")
    server.serve_forever()
