from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from midbrain_bufferref import copy_buffer_refs


@dataclass(frozen=True)
class EvidenceFrame:
    rgb: np.ndarray
    depth_m: np.ndarray
    mask: np.ndarray
    source: dict[str, Any]


def _decode_rgb(reference: dict[str, Any], payload: bytes) -> np.ndarray:
    height, width = int(reference["height"]), int(reference["width"])
    format_name = str(reference.get("format_name") or "").upper()
    if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
        from io import BytesIO

        return np.asarray(Image.open(BytesIO(payload)).convert("RGB"), dtype=np.uint8)
    channels = 4 if format_name in {"RGBA", "BGRA"} else 3
    image = np.frombuffer(payload, np.uint8).reshape(height, width, channels)
    if format_name == "RGB":
        return image.copy()
    if format_name == "BGR":
        return image[:, :, ::-1].copy()
    if format_name == "RGBA":
        return image[:, :, :3].copy()
    if format_name == "BGRA":
        return image[:, :, [2, 1, 0]].copy()
    raise ValueError(f"unsupported RGB BufferRef format {format_name!r}")


def _decode_depth(reference: dict[str, Any], payload: bytes) -> np.ndarray:
    height, width = int(reference["height"]), int(reference["width"])
    format_name = str(reference.get("format_name") or "").upper()
    if format_name not in {"Y16", "DEPTH16", "Z16"}:
        raise ValueError(f"unsupported depth BufferRef format {format_name!r}")
    values = np.frombuffer(payload, "<u2", count=height * width)
    if values.size != height * width:
        raise ValueError("depth BufferRef payload is shorter than declared")
    scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
    return values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0


def _load_mask(specification: dict[str, Any], shape: tuple[int, int]) -> np.ndarray:
    path = Path(str(specification.get("path") or "")).resolve()
    if not path.is_file():
        raise ValueError(f"mask file is unavailable: {path}")
    mask = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    if mask.shape != shape:
        raise ValueError(f"mask shape {mask.shape} does not match RGB-D shape {shape}")
    return np.ascontiguousarray(mask > 0, dtype=np.uint8)


def load_evidence(specification: dict[str, Any]) -> EvidenceFrame:
    rgb_ref = specification.get("rgb_buffer_ref")
    depth_ref = specification.get("depth_buffer_ref")
    if isinstance(rgb_ref, dict) and isinstance(depth_ref, dict):
        rgb_payload, depth_payload = copy_buffer_refs([rgb_ref, depth_ref])
        rgb = _decode_rgb(rgb_ref, rgb_payload)
        depth = _decode_depth(depth_ref, depth_payload)
        source = {"kind": "CAMERA_BUFFER_REFS", "rgb": rgb_ref, "depth": depth_ref}
    else:
        rgb_path = Path(str(specification.get("rgb_path") or "")).resolve()
        depth_path = Path(str(specification.get("depth_npy_path") or "")).resolve()
        if not rgb_path.is_file() or not depth_path.is_file():
            raise ValueError(
                "evidence requires RGB/depth BufferRefs or rgb_path and depth_npy_path"
            )
        rgb = np.asarray(Image.open(rgb_path).convert("RGB"), dtype=np.uint8)
        depth = np.asarray(np.load(depth_path, allow_pickle=False), dtype=np.float32)
        source = {"kind": "REPLAY_FILES", "rgb_path": str(rgb_path), "depth_path": str(depth_path)}
    if rgb.ndim != 3 or rgb.shape[2] != 3 or depth.shape != rgb.shape[:2]:
        raise ValueError("RGB and depth evidence shapes are incompatible")
    mask_spec = specification.get("mask")
    if not isinstance(mask_spec, dict):
        raise ValueError("evidence requires a mask file specification")
    mask = _load_mask(mask_spec, rgb.shape[:2])
    return EvidenceFrame(
        rgb=np.ascontiguousarray(rgb, dtype=np.uint8),
        depth_m=np.ascontiguousarray(depth, dtype=np.float32),
        mask=mask,
        source=source,
    )
