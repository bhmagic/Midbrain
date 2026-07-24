from __future__ import annotations

import unittest

import httpx

from stationary_world_arm_alignment.clients import FabricClient


class FabricClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_recent_stream_is_empty_history(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "stream not found"})

        client = FabricClient("http://fabric.test")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(await client.recent("perception.object.pose"), [])
        finally:
            await client.close()
