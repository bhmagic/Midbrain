from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from vegetable_cutting.camera import RgbdCapture


class TransientFabric:
    def __init__(
        self,
        *,
        failures: int,
        value: dict[str, Any] | None,
    ) -> None:
        self.failures = failures
        self.value = value
        self.calls = 0

    async def latest_optional(self, _: str) -> dict[str, Any] | None:
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ReadTimeout("synthetic local Fabric delay")
        return self.value


def test_camera_fabric_read_retries_transient_timeout() -> None:
    fabric = TransientFabric(failures=2, value={"stream": "camera.rgbd.bundle"})
    capture = RgbdCapture(fabric, "camera")  # type: ignore[arg-type]

    result = asyncio.run(
        capture._latest_optional_with_retry(
            "camera.rgbd.bundle",
            attempts=3,
            retry_delay_s=0.0,
            timeout_s=0.1,
        )
    )

    assert result == {"stream": "camera.rgbd.bundle"}
    assert fabric.calls == 3


def test_camera_fabric_read_reports_exception_type_after_retry_budget() -> None:
    fabric = TransientFabric(failures=3, value=None)
    capture = RgbdCapture(fabric, "camera")  # type: ignore[arg-type]

    with pytest.raises(
        RuntimeError,
        match=r"camera\.calibration.*ReadTimeout.*synthetic local Fabric delay",
    ):
        asyncio.run(
            capture._latest_optional_with_retry(
                "camera.calibration",
                attempts=3,
                retry_delay_s=0.0,
                timeout_s=0.1,
            )
        )
