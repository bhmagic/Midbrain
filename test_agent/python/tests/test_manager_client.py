from __future__ import annotations

import asyncio
import unittest

import httpx

from physical_agent_test.manager_client import ManagerClient, _manager_error_detail


class _Client:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"requested_url": url},
        )


class ManagerClientErrorTests(unittest.TestCase):
    def test_preserves_manager_json_error_detail(self) -> None:
        response = httpx.Response(
            503,
            request=httpx.Request(
                "POST",
                "http://127.0.0.1:7001/v1/workcell-calibrations/activate",
            ),
            json={
                "error": (
                    "current VIO status is unavailable: Fabric returned "
                    "404 Not Found"
                )
            },
        )

        self.assertEqual(
            _manager_error_detail(response),
            (
                "current VIO status is unavailable: Fabric returned "
                "404 Not Found"
            ),
        )

    def test_reads_compact_catalog_and_quotes_provider_detail_identity(
        self,
    ) -> None:
        manager = ManagerClient("http://127.0.0.1:7001")
        client = _Client()
        asyncio.run(manager._client.aclose())
        manager._client = client  # type: ignore[assignment]

        catalog = asyncio.run(manager.agent_runtime_catalog())
        detail = asyncio.run(manager.provider_detail("provider/with space"))

        self.assertIn("/v1/agent-runtime-catalog", catalog["requested_url"])
        self.assertTrue(
            detail["requested_url"].endswith(
                "/v1/providers/provider%2Fwith%20space/detail"
            )
        )


if __name__ == "__main__":
    unittest.main()
