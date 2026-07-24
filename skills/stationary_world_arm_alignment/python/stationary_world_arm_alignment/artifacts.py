from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MonitorArtifacts:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rgb_jpeg: bytes | None = None
    depth_png: bytes | None = None
    overlay_jpeg: bytes | None = None
    geometry: dict[str, Any] = field(default_factory=lambda: {"frames": [], "points": []})

    async def set_images(
        self,
        *,
        rgb_jpeg: bytes,
        depth_png: bytes,
        overlay_jpeg: bytes | None = None,
    ) -> None:
        async with self.lock:
            self.rgb_jpeg = rgb_jpeg
            self.depth_png = depth_png
            if overlay_jpeg is not None:
                self.overlay_jpeg = overlay_jpeg

    async def set_overlay(self, payload: bytes) -> None:
        async with self.lock:
            self.overlay_jpeg = payload

    async def set_geometry(self, payload: dict[str, Any]) -> None:
        async with self.lock:
            self.geometry = payload

    async def image(self, kind: str) -> bytes | None:
        async with self.lock:
            return {
                "rgb": self.rgb_jpeg,
                "depth": self.depth_png,
                "overlay": self.overlay_jpeg,
            }.get(kind)

    async def geometry_snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return dict(self.geometry)
