from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import patch

import pytest

from refine_arm_root_translation.rpc_entrypoint import LineRpcClient, LineRpcError


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
