from __future__ import annotations

import unittest

import httpx

from physical_agent_test.manager_client import _manager_error_detail


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


if __name__ == "__main__":
    unittest.main()
