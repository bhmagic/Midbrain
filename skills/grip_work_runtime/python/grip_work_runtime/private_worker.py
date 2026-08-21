from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable
import uuid


class HostRpcClient:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def call(
        self,
        service: str,
        method: str,
        **parameters: Any,
    ) -> Any:
        async with self._lock:
            request_id = str(uuid.uuid4())
            request = {
                "type": "request",
                "id": request_id,
                "service": service,
                "method": method,
                "parameters": parameters,
            }
            sys.stdout.write(json.dumps(request) + "\n")
            sys.stdout.flush()
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                raise RuntimeError("Grip Skill host RPC closed before responding")
            response = json.loads(line)
            if response.get("type") != "response" or response.get("id") != request_id:
                raise RuntimeError("Grip Skill host RPC returned a mismatched response")
            if response.get("ok") is not True:
                error = response.get("error")
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                raise RuntimeError(str(error.get("message") or error))
            return response.get("result")


class ManagerProxy:
    def __init__(self, rpc: HostRpcClient, base_url: str) -> None:
        self.rpc = rpc
        self.base_url = base_url.rstrip("/")

    async def set_hot(self, provider_id: str) -> dict[str, Any]:
        return await self.rpc.call(
            "manager",
            "set_hot",
            provider_id=provider_id,
        )

    async def set_residency(
        self,
        provider_id: str,
        action: str,
    ) -> dict[str, Any]:
        return await self.rpc.call(
            "manager",
            "set_residency",
            provider_id=provider_id,
            action=action,
        )

    async def workcell_calibrations(self) -> dict[str, Any]:
        return await self.rpc.call("manager", "workcell_calibrations")


class IntegratedMotionProxy:
    def __init__(self, rpc: HostRpcClient) -> None:
        self.rpc = rpc

    async def observation(self) -> dict[str, Any]:
        return await self.rpc.call("integrated_motion", "observation")

    async def preview(self, **arguments: Any) -> dict[str, Any]:
        return await self.rpc.call(
            "integrated_motion",
            "preview",
            **arguments,
        )

    async def execute_preview(self, *, preview_id: str) -> dict[str, Any]:
        return await self.rpc.call(
            "integrated_motion",
            "execute_preview",
            preview_id=preview_id,
        )


async def _run(
    builder: Callable[[Path, dict[str, Any], Any], Any],
) -> None:
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        raise RuntimeError("Grip Skill private worker received no invocation")
    invocation = json.loads(line)
    if invocation.get("type") != "invoke":
        raise RuntimeError("Grip Skill private worker received invalid invocation")
    arguments = invocation.get("arguments")
    context = invocation.get("context")
    if not isinstance(arguments, dict) or not isinstance(context, dict):
        raise RuntimeError("Grip Skill private invocation must contain objects")
    rpc = HostRpcClient()
    services = SimpleNamespace(
        manager=ManagerProxy(rpc, str(context["manager_url"])),
        integrated_motion=IntegratedMotionProxy(rpc),
    )
    adapter = builder(Path.cwd().resolve(), context, services)
    result = adapter.invoke(arguments)
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        raise RuntimeError("Grip Skill workflow returned a non-object")
    sys.stdout.write(json.dumps({"type": "result", "ok": True, "result": result}) + "\n")
    sys.stdout.flush()


def run_private_worker(
    builder: Callable[[Path, dict[str, Any], Any], Any],
) -> None:
    try:
        asyncio.run(_run(builder))
    except Exception as error:
        sys.stdout.write(
            json.dumps(
                {
                    "type": "result",
                    "ok": False,
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        raise SystemExit(1) from error
