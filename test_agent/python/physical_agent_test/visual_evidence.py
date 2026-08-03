from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


VISUAL_EVIDENCE_SCHEMA = "midbrain.visual_evidence"
VISUAL_EVIDENCE_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SAFE_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class StoredVisualChannel:
    data: bytes
    media_type: str
    width: int
    height: int


@dataclass
class _StoredVisualEvidence:
    created_monotonic: float
    channels: dict[str, StoredVisualChannel]


class VisualEvidenceStore:
    """Bounded in-memory storage for exact images referenced by UI events."""

    def __init__(
        self,
        *,
        retention_s: float = 1800.0,
        maximum_items: int = 128,
    ):
        self.retention_s = float(retention_s)
        self.maximum_items = max(1, int(maximum_items))
        self._items: dict[str, _StoredVisualEvidence] = {}
        self._lock = asyncio.Lock()

    async def register_rgb(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        width: int,
        height: int,
        title: str,
        annotations: list[dict[str, Any]],
        confidence: str,
        model: str,
        source_skill: str,
    ) -> dict[str, Any]:
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError(f"unsupported visual evidence media type: {media_type}")
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("visual evidence dimensions must be positive")
        evidence_id = uuid4().hex
        channel = StoredVisualChannel(
            data=bytes(image_bytes),
            media_type=media_type,
            width=int(width),
            height=int(height),
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
            self._items[evidence_id] = _StoredVisualEvidence(
                created_monotonic=time.monotonic(),
                channels={"rgb": channel},
            )
        return {
            "schema": VISUAL_EVIDENCE_SCHEMA,
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "title": str(title)[:200],
            "default_channel": "rgb",
            "channels": [
                {
                    "id": "rgb",
                    "label": "RGB",
                    "url": (
                        f"/api/visual-evidence/{evidence_id}/channels/rgb"
                    ),
                    "media_type": media_type,
                    "width": int(width),
                    "height": int(height),
                    "sha256": hashlib.sha256(image_bytes).hexdigest(),
                }
            ],
            "annotation_space": {
                "units": "normalized",
                "origin": "top_left",
                "x_axis": "right",
                "y_axis": "down",
            },
            "annotations": [dict(annotation) for annotation in annotations],
            "confidence": str(confidence),
            "model": str(model)[:100],
            "source_skill": str(source_skill)[:160],
        }

    async def read(
        self,
        evidence_id: str,
        channel_id: str,
    ) -> StoredVisualChannel:
        async with self._lock:
            self._prune_locked()
            item = self._items.get(str(evidence_id))
            if item is None:
                raise KeyError(evidence_id)
            channel = item.channels.get(str(channel_id))
            if channel is None:
                raise KeyError(channel_id)
            return channel

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            evidence_id
            for evidence_id, item in self._items.items()
            if now - item.created_monotonic > self.retention_s
        ]
        for evidence_id in expired:
            self._items.pop(evidence_id, None)


def sanitize_visual_evidence(value: Any) -> dict[str, Any] | None:
    """Project a tool result onto the safe browser visual-evidence subset."""

    if not isinstance(value, dict):
        return None
    if value.get("schema") != VISUAL_EVIDENCE_SCHEMA:
        return None
    if value.get("schema_version") != VISUAL_EVIDENCE_SCHEMA_VERSION:
        return None
    evidence_id = str(value.get("evidence_id") or "")
    if _SAFE_ID.fullmatch(evidence_id) is None:
        return None
    raw_channels = value.get("channels")
    if not isinstance(raw_channels, list):
        return None
    channels: list[dict[str, Any]] = []
    for candidate in raw_channels[:8]:
        if not isinstance(candidate, dict):
            continue
        channel_id = str(candidate.get("id") or "")
        if _SAFE_ID.fullmatch(channel_id) is None:
            continue
        expected_url = (
            f"/api/visual-evidence/{evidence_id}/channels/{channel_id}"
        )
        if candidate.get("url") != expected_url:
            continue
        media_type = str(candidate.get("media_type") or "")
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            continue
        try:
            width = int(candidate.get("width"))
            height = int(candidate.get("height"))
        except (TypeError, ValueError):
            continue
        digest = str(candidate.get("sha256") or "")
        if width <= 0 or height <= 0 or _SAFE_SHA256.fullmatch(digest) is None:
            continue
        channels.append(
            {
                "id": channel_id,
                "label": str(candidate.get("label") or channel_id)[:40],
                "url": expected_url,
                "media_type": media_type,
                "width": width,
                "height": height,
                "sha256": digest,
            }
        )
    if not channels:
        return None
    channel_ids = {channel["id"] for channel in channels}
    default_channel = str(value.get("default_channel") or "")
    if default_channel not in channel_ids:
        default_channel = str(channels[0]["id"])
    raw_annotations = value.get("annotations")
    if not isinstance(raw_annotations, list):
        raw_annotations = []
    annotations: list[dict[str, Any]] = []
    for candidate in raw_annotations[:64]:
        sanitized = _sanitize_annotation(candidate, channel_ids)
        if sanitized is not None:
            annotations.append(sanitized)
    confidence = str(value.get("confidence") or "unknown").lower()
    if confidence not in {"low", "medium", "high", "unknown"}:
        confidence = "unknown"
    return {
        "schema": VISUAL_EVIDENCE_SCHEMA,
        "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "title": str(value.get("title") or "Visual evidence")[:200],
        "default_channel": default_channel,
        "channels": channels,
        "annotation_space": {
            "units": "normalized",
            "origin": "top_left",
            "x_axis": "right",
            "y_axis": "down",
        },
        "annotations": annotations,
        "confidence": confidence,
        "model": str(value.get("model") or "")[:100],
        "source_skill": str(value.get("source_skill") or "")[:160],
    }


def _sanitize_annotation(
    value: Any,
    channel_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    annotation_type = str(value.get("type") or "").lower()
    annotation_id = str(value.get("id") or "")
    if _SAFE_ID.fullmatch(annotation_id) is None:
        return None
    raw_applies_to = value.get("applies_to_channels")
    if not isinstance(raw_applies_to, list):
        return None
    channels = [
        str(channel_id)
        for channel_id in raw_applies_to
        if str(channel_id) in channel_ids
    ]
    if not channels:
        return None
    geometry: dict[str, float]
    if annotation_type == "point":
        x = _normalized_number(value.get("x"))
        y = _normalized_number(value.get("y"))
        if x is None or y is None:
            return None
        geometry = {"x": x, "y": y}
    elif annotation_type == "box":
        x = _normalized_number(value.get("x"))
        y = _normalized_number(value.get("y"))
        width = _normalized_number(value.get("width"))
        height = _normalized_number(value.get("height"))
        if None in {x, y, width, height}:
            return None
        assert x is not None and y is not None
        assert width is not None and height is not None
        if width <= 0.0 or height <= 0.0 or x + width > 1.0 or y + height > 1.0:
            return None
        geometry = {"x": x, "y": y, "width": width, "height": height}
    else:
        return None
    confidence = str(value.get("confidence") or "unknown").lower()
    if confidence not in {"low", "medium", "high", "unknown"}:
        confidence = "unknown"
    return {
        "id": annotation_id,
        "type": annotation_type,
        "label": str(value.get("label") or annotation_type)[:120],
        "confidence": confidence,
        "applies_to_channels": list(dict.fromkeys(channels)),
        **geometry,
    }


def _normalized_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None
