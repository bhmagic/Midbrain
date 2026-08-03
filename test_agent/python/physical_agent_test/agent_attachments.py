from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PIL import Image


_SUPPORTED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class AgentImageAttachment:
    attachment_id: str
    data: bytes
    media_type: str
    width: int
    height: int
    filename: str
    sha256: str

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def public_metadata(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "kind": "image",
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": len(self.data),
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "preview_url": f"/api/agent-attachments/{self.attachment_id}",
        }


@dataclass
class _StoredAttachment:
    created_monotonic: float
    attachment: AgentImageAttachment


class AgentAttachmentStore:
    """Bounded in-memory storage for user-supplied Agent image inputs."""

    def __init__(
        self,
        *,
        retention_s: float = 1800.0,
        maximum_items: int = 64,
        maximum_bytes: int = 8 * 1024 * 1024,
        maximum_pixels: int = 40_000_000,
    ):
        self.retention_s = max(1.0, float(retention_s))
        self.maximum_items = max(1, int(maximum_items))
        self.maximum_bytes = max(1, int(maximum_bytes))
        self.maximum_pixels = max(1, int(maximum_pixels))
        self._items: dict[str, _StoredAttachment] = {}
        self._lock = asyncio.Lock()

    async def register_base64(
        self,
        *,
        data_base64: str,
        media_type: str,
        filename: str,
    ) -> AgentImageAttachment:
        estimated_size = (len(data_base64) * 3) // 4
        if estimated_size > self.maximum_bytes + 2:
            raise ValueError(
                f"image attachment exceeds {self.maximum_bytes} bytes"
            )
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("image attachment is not valid base64") from error
        if not data:
            raise ValueError("image attachment is empty")
        if len(data) > self.maximum_bytes:
            raise ValueError(
                f"image attachment exceeds {self.maximum_bytes} bytes"
            )
        canonical_media_type, width, height = self._inspect_image(data)
        if media_type != canonical_media_type:
            raise ValueError(
                "declared image media type does not match the encoded image"
            )
        attachment_id = uuid4().hex
        attachment = AgentImageAttachment(
            attachment_id=attachment_id,
            data=data,
            media_type=canonical_media_type,
            width=width,
            height=height,
            filename=self._safe_filename(filename, canonical_media_type),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        async with self._lock:
            self._prune_locked()
            while len(self._items) >= self.maximum_items:
                oldest_id = min(
                    self._items,
                    key=lambda item_id: self._items[
                        item_id
                    ].created_monotonic,
                )
                self._items.pop(oldest_id, None)
            self._items[attachment_id] = _StoredAttachment(
                created_monotonic=time.monotonic(),
                attachment=attachment,
            )
        return attachment

    async def read(self, attachment_id: str) -> AgentImageAttachment:
        if _SAFE_ID.fullmatch(str(attachment_id)) is None:
            raise KeyError(attachment_id)
        async with self._lock:
            self._prune_locked()
            stored = self._items.get(str(attachment_id))
            if stored is None:
                raise KeyError(attachment_id)
            return stored.attachment

    async def remove(self, attachment_id: str) -> bool:
        async with self._lock:
            return self._items.pop(str(attachment_id), None) is not None

    def _inspect_image(self, data: bytes) -> tuple[str, int, int]:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                media_type = _SUPPORTED_FORMATS.get(image_format)
                width = int(image.width)
                height = int(image.height)
                animated = bool(getattr(image, "is_animated", False))
                if media_type is None:
                    raise ValueError("unsupported image format")
                if width <= 0 or height <= 0:
                    raise ValueError("image dimensions must be positive")
                if width * height > self.maximum_pixels:
                    raise ValueError(
                        f"image exceeds {self.maximum_pixels} decoded pixels"
                    )
                if animated:
                    raise ValueError("animated image attachments are not supported")
                image.verify()
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError("image attachment could not be decoded") from error
        return media_type, width, height

    @staticmethod
    def _safe_filename(filename: str, media_type: str) -> str:
        candidate = Path(str(filename)).name.strip()
        candidate = "".join(
            character
            for character in candidate
            if character.isprintable() and character not in "\r\n\t"
        )[:160]
        if candidate:
            return candidate
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }[media_type]
        return f"user-image{suffix}"

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            attachment_id
            for attachment_id, stored in self._items.items()
            if now - stored.created_monotonic > self.retention_s
        ]
        for attachment_id in expired:
            self._items.pop(attachment_id, None)


def build_multimodal_user_input(
    prompt: str,
    attachments: list[AgentImageAttachment],
) -> str | list[dict[str, object]]:
    if not attachments:
        return prompt
    content: list[dict[str, object]] = [
        {"type": "input_text", "text": prompt}
    ]
    content.extend(
        {
            "type": "input_image",
            "image_url": attachment.data_url(),
            "detail": "auto",
        }
        for attachment in attachments
    )
    return [
        {
            "type": "message",
            "role": "user",
            "content": content,
        }
    ]
