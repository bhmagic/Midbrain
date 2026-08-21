from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import subprocess
from typing import Any


_ALLOWED_CALLS = {
    "manager": {
        "set_hot",
        "set_residency",
        "workcell_calibrations",
    },
    "integrated_motion": {
        "observation",
        "preview",
        "execute_preview",
    },
}


class PrivateSkillProcessAdapter:
    """Run a finite Grip Skill inside its Skill-owned virtual environment."""

    def __init__(
        self,
        *,
        skill_root: Path,
        worker_entrypoint: Path,
        services: Any,
    ) -> None:
        self.skill_root = skill_root.resolve()
        self.worker_entrypoint = worker_entrypoint.resolve()
        self.services = services
        self.python_path = self._private_python(self.skill_root)
        self.lock = asyncio.Lock()

    @staticmethod
    def _private_python(skill_root: Path) -> Path:
        candidates = (
            skill_root / ".venv" / "Scripts" / "python.exe",
            skill_root / ".venv" / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise RuntimeError(
            f"Skill-private Python is unavailable under {skill_root / '.venv'}"
        )

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            return await self._invoke_private(arguments)

    async def _invoke_private(self, arguments: dict[str, Any]) -> dict[str, Any]:
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        process = await asyncio.create_subprocess_exec(
            str(self.python_path),
            str(self.worker_entrypoint),
            "--private-worker",
            cwd=str(self.skill_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            limit=8 * 1024 * 1024,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        request = {
            "type": "invoke",
            "arguments": arguments,
            "context": {
                "manager_url": str(
                    getattr(
                        getattr(self.services, "manager", None),
                        "base_url",
                        "http://127.0.0.1:7001",
                    )
                ),
                "contact_url": str(
                    getattr(
                        self.services,
                        "contact_provider_url",
                        "http://127.0.0.1:8794",
                    )
                ),
                "grip_url": str(
                    getattr(
                        self.services,
                        "grip_provider_url",
                        "http://127.0.0.1:8795",
                    )
                ),
                "integrated_url": str(
                    getattr(
                        self.services,
                        "integrated_provider_url",
                        "http://127.0.0.1:8793",
                    )
                ),
                "authorization_url": str(
                    getattr(
                        self.services,
                        "authorization_url",
                        "http://127.0.0.1:8000",
                    )
                ),
            },
        }
        process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await process.stdin.drain()
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    stderr = await self._stderr(process)
                    raise RuntimeError(
                        "Grip Skill private worker exited without a result"
                        + (f": {stderr}" if stderr else "")
                    )
                message = json.loads(line.decode("utf-8"))
                message_type = message.get("type")
                if message_type == "request":
                    response = await self._dispatch(message)
                    process.stdin.write(
                        (json.dumps(response) + "\n").encode("utf-8")
                    )
                    await process.stdin.drain()
                    continue
                if message_type != "result":
                    raise RuntimeError("Grip Skill private worker emitted invalid RPC")
                await process.wait()
                if message.get("ok") is not True:
                    error = message.get("error")
                    if not isinstance(error, dict):
                        error = {"message": str(error)}
                    raise RuntimeError(str(error.get("message") or error))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Grip Skill private worker returned a non-object")
                return result
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        service_name = str(message.get("service") or "")
        method_name = str(message.get("method") or "")
        parameters = message.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        try:
            allowed = _ALLOWED_CALLS.get(service_name, set())
            if method_name not in allowed:
                raise RuntimeError(
                    f"Grip Skill RPC is not allowed: {service_name}.{method_name}"
                )
            target = getattr(self.services, service_name, None)
            if target is None:
                raise RuntimeError(
                    f"Grip Skill requires unavailable host service {service_name}"
                )
            operation = getattr(target, method_name, None)
            if not callable(operation):
                raise RuntimeError(
                    f"Grip Skill host service has no {service_name}.{method_name}"
                )
            result = operation(**parameters)
            if inspect.isawaitable(result):
                result = await result
            return {"type": "response", "id": request_id, "ok": True, "result": result}
        except Exception as error:
            return {
                "type": "response",
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }

    @staticmethod
    async def _stderr(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        raw = await process.stderr.read()
        return raw.decode("utf-8", errors="replace").strip()[-4000:]


def build_private_adapter(
    *,
    skill_root: Path,
    worker_entrypoint: Path,
    services: Any,
) -> PrivateSkillProcessAdapter:
    return PrivateSkillProcessAdapter(
        skill_root=skill_root,
        worker_entrypoint=worker_entrypoint,
        services=services,
    )
