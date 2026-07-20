from __future__ import annotations

import io
import sys
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .fabric_client import FabricClient
from orbbec_femto_provider.shared_memory_access import CameraSharedMemory


@dataclass(frozen=True)
class CapturedDepth:
    image_bytes: bytes
    mime_type: str
    path: Path
    observation: dict[str, Any]


class DepthCapture:
    """Read the exact Y16 depth BufferRef and render a diagnostic PNG."""

    def __init__(self, fabric: FabricClient, screenshot_dir: Path):
        self.fabric = fabric
        self.screenshot_dir = screenshot_dir

    async def capture_latest(self) -> CapturedDepth:
        last_error: Exception | None = None
        for _ in range(3):
            observation = await self.fabric.latest("camera.depth.frame_ref")
            reference = observation["data"]
            reader = CameraSharedMemory(reference["mapping_name"]).open()
            try:
                raw = reader.read_ref(reference)
            except Exception as error:
                last_error = error
                continue
            finally:
                reader.close()

            image_bytes = self._render_depth_png(raw, reference)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            path = self.screenshot_dir / f"depth_{stamp}.png"
            path.write_bytes(image_bytes)
            return CapturedDepth(
                image_bytes=image_bytes,
                mime_type="image/png",
                path=path,
                observation=observation,
            )
        raise RuntimeError(
            f"latest depth BufferRef expired before it could be read: {last_error}"
        )

    @staticmethod
    def _render_depth_png(payload: bytes, reference: dict[str, Any]) -> bytes:
        format_name = str(reference.get("format_name", "")).upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported depth frame format: {format_name or 'unknown'}")

        width = int(reference["width"])
        height = int(reference["height"])
        expected_bytes = width * height * 2
        if len(payload) < expected_bytes:
            raise RuntimeError(
                f"depth payload is short: expected {expected_bytes}, got {len(payload)}"
            )

        values = array("H")
        values.frombytes(payload[:expected_bytes])
        if sys.byteorder != "little":
            values.byteswap()

        valid = [value for value in values if value > 0]
        if not valid:
            rendered = bytes(width * height)
        else:
            near_mm = max(min(valid), 200)
            far_mm = min(max(valid), 5000)
            if far_mm <= near_mm:
                far_mm = near_mm + 1
            scale = 255.0 / float(far_mm - near_mm)
            rendered = bytes(
                0
                if value == 0
                else max(0, min(255, int((far_mm - value) * scale)))
                for value in values
            )

        image = Image.frombytes("L", (width, height), rendered)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
