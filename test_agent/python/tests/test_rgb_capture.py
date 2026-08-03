from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from physical_agent_test.rgb_capture import (
    CameraObservationUnavailable,
    RgbCapture,
)


class _Fabric:
    def __init__(self, rgb_observations: list[dict | None]):
        self.rgb_observations = list(rgb_observations)
        self.rgb_requests = 0

    async def latest_optional(self, stream: str):
        if stream == "camera.rgbd.data_routes":
            return None
        if stream != "camera.rgb.frame_ref":
            raise AssertionError(f"unexpected stream: {stream}")
        self.rgb_requests += 1
        if len(self.rgb_observations) > 1:
            return self.rgb_observations.pop(0)
        return self.rgb_observations[0]


class _Reader:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.closed = False

    def open(self):
        return self

    def read_ref(self, _reference):
        return self.payload

    def close(self):
        self.closed = True


class RgbCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_first_frame_after_provider_activation(self) -> None:
        observation = {
            "provider_id": "camera.femto_bolt",
            "frame_id": "color:1",
            "data": {
                "mapping_name": "Local\\test-camera",
                "format_name": "MJPG",
                "width": 1920,
                "height": 1080,
            },
        }
        fabric = _Fabric([None, None, observation])
        reader = _Reader(b"jpeg-frame")
        with tempfile.TemporaryDirectory() as temporary_directory, patch(
            "physical_agent_test.rgb_capture.CameraSharedMemory",
            return_value=reader,
        ):
            capture = RgbCapture(
                fabric,
                Path(temporary_directory),
                first_frame_timeout_s=1.0,
                retry_interval_s=0.0,
            )
            result = await capture.capture_latest(
                provider_id="camera.femto_bolt",
                binding_id="binding-1",
            )

        self.assertEqual(result.image_bytes, b"jpeg-frame")
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertEqual(fabric.rgb_requests, 3)
        self.assertTrue(reader.closed)

    async def test_reports_bounded_timeout_when_no_frame_arrives(self) -> None:
        fabric = _Fabric([None])
        with tempfile.TemporaryDirectory() as temporary_directory:
            capture = RgbCapture(
                fabric,
                Path(temporary_directory),
                first_frame_timeout_s=0.0,
                retry_interval_s=0.0,
            )
            with self.assertRaisesRegex(
                CameraObservationUnavailable,
                "did not become readable within 0.0 seconds",
            ):
                await capture.capture_latest(
                    provider_id="camera.femto_bolt"
                )


if __name__ == "__main__":
    unittest.main()
