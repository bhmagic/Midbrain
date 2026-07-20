from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .fabric_client import FabricClient
from orbbec_femto_provider.shared_memory_access import CameraSharedMemory


@dataclass(frozen=True)
class CapturedRgb:
    image_bytes: bytes
    mime_type: str
    path: Path
    observation: dict[str, Any]


class RgbCapture:
    def __init__(self, fabric: FabricClient, screenshot_dir: Path):
        self.fabric = fabric
        self.screenshot_dir = screenshot_dir

    async def capture_latest(self) -> CapturedRgb:
        last_error: Exception | None = None
        for _ in range(3):
            observation = await self.fabric.latest("camera.rgb.frame_ref")
            reference = observation["data"]
            mapping_name = reference["mapping_name"]
            reader = CameraSharedMemory(mapping_name).open()
            try:
                raw = reader.read_ref(reference)
            except Exception as error:
                last_error = error
                continue
            finally:
                reader.close()
            image_bytes, mime_type = self._normalize_to_jpeg(raw, reference)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            path = self.screenshot_dir / f"rgb_{stamp}.jpg"
            path.write_bytes(image_bytes)
            return CapturedRgb(
                image_bytes=image_bytes,
                mime_type=mime_type,
                path=path,
                observation=observation,
            )
        raise RuntimeError(f"latest RGB BufferRef expired before it could be read: {last_error}")

    @staticmethod
    def _normalize_to_jpeg(payload: bytes, reference: dict[str, Any]) -> tuple[bytes, str]:
        format_name = str(reference.get("format_name", "")).upper()
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            return payload, "image/jpeg"

        width = int(reference["width"])
        height = int(reference["height"])
        if format_name == "RGB":
            image = Image.frombytes("RGB", (width, height), payload)
        elif format_name == "BGR":
            image = Image.frombytes("RGB", (width, height), payload, "raw", "BGR")
        elif format_name == "RGBA":
            image = Image.frombytes("RGBA", (width, height), payload).convert("RGB")
        elif format_name == "BGRA":
            image = Image.frombytes("RGBA", (width, height), payload, "raw", "BGRA").convert("RGB")
        else:
            raise RuntimeError(f"unsupported RGB frame format: {format_name or 'unknown'}")

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92)
        return output.getvalue(), "image/jpeg"
