from __future__ import annotations

import asyncio
import io
import json
import queue
from unittest.mock import patch

import pytest

from refine_arm_root_translation.rpc_entrypoint import LineRpcClient, LineRpcError


class _QueuedInput:
    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def readline(self) -> str:
        return self.lines.get(timeout=1.0)

    def send(self, response: dict) -> None:
        self.lines.put(json.dumps(response) + "\n")


def test_line_rpc_client_preserves_structured_host_error() -> None:
    response = {
        "id": 1,
        "ok": False,
        "error": {
            "type": "HTTPStatusError",
            "message": "409 Conflict",
            "status_code": 409,
            "response_body": {"error": "active arm identity changed"},
        },
    }
    stdin = io.StringIO(json.dumps(response) + "\n")
    stdout = io.StringIO()

    with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
        with pytest.raises(LineRpcError) as captured:
            asyncio.run(LineRpcClient().request("manager.update", {}))

    assert captured.value.status_code == 409
    assert captured.value.error_type == "HTTPStatusError"
    assert captured.value.response_body == {
        "error": "active arm identity changed"
    }
    request = json.loads(stdout.getvalue())
    assert request["id"] == 1
    assert request["method"] == "manager.update"


def test_line_rpc_client_correlates_out_of_order_responses() -> None:
    stdin = _QueuedInput()
    stdout = io.StringIO()

    async def exercise() -> list[str]:
        client = LineRpcClient()
        first = asyncio.create_task(client.request("vlm.invoke", {"sample": 1}))
        second = asyncio.create_task(client.request("vlm.invoke", {"sample": 2}))
        while len(stdout.getvalue().splitlines()) < 2:
            await asyncio.sleep(0)
        stdin.send({"id": 2, "ok": True, "result": "second"})
        stdin.send({"id": 1, "ok": True, "result": "first"})
        return list(await asyncio.gather(first, second))

    with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
        results = asyncio.run(exercise())

    assert results == ["first", "second"]
    requests = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [request["id"] for request in requests] == [1, 2]
