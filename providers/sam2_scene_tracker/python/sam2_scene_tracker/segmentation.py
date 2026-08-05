from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import cv2
import numpy as np

from .policy import SceneSegmentationPolicy


@dataclass(frozen=True)
class MaskPartition:
    object_masks: dict[str, np.ndarray]
    object_types: dict[str, str]
    dilated_arm_mask: np.ndarray
    pushable_mask: np.ndarray
    diagnostics: dict[str, Any]


def _mask(value: Any, shape: tuple[int, int], label: str) -> np.ndarray:
    result = np.asarray(value, dtype=bool)
    if result.shape != shape:
        raise ValueError(f"{label} must have shape {shape}")
    return np.ascontiguousarray(result)


def dilate_binary_mask(mask: np.ndarray, dilation_pixels: int) -> np.ndarray:
    pixels = int(dilation_pixels)
    if pixels < 0 or pixels > 256:
        raise ValueError("arm mask dilation must be between 0 and 256 pixels")
    binary = np.asarray(mask, dtype=bool)
    if pixels == 0:
        return np.ascontiguousarray(binary)
    size = pixels * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return np.ascontiguousarray(
        cv2.dilate(binary.astype(np.uint8), kernel, iterations=1) > 0
    )


def constrain_mask_to_prompted_depth_component(
    *,
    mask: np.ndarray,
    depth_m: np.ndarray,
    boxes_yxyx: list[tuple[int, int, int, int]],
    positive_points_yx: list[tuple[int, int]],
    box_padding_fraction: float = 0.03,
    local_depth_step_m: float = 0.035,
    seed_search_radius_pixels: int = 24,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Keep only VLM-bounded, depth-connected SAM2 regions.

    SAM2 is allowed to refine the target boundary, but it may not extend a
    semantic label outside the VLM regions that introduced that label. A
    floating-range flood fill then rejects surfaces separated from the VLM
    positive seeds by a depth discontinuity, such as the floor behind a table
    or a workpiece sitting above it.
    """

    binary = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    depth = np.asarray(depth_m, dtype=np.float32)
    if binary.ndim != 2 or depth.shape != binary.shape:
        raise ValueError("semantic mask and depth must share one 2D shape")
    padding = float(box_padding_fraction)
    if not math.isfinite(padding) or not 0.0 <= padding <= 0.25:
        raise ValueError("box_padding_fraction must be in [0, 0.25]")
    threshold = float(local_depth_step_m)
    if not math.isfinite(threshold) or not 0.001 <= threshold <= 0.25:
        raise ValueError("local_depth_step_m must be in [0.001, 0.25]")
    search_radius = int(seed_search_radius_pixels)
    if not 0 <= search_radius <= 256:
        raise ValueError("seed_search_radius_pixels must be in [0, 256]")

    height, width = binary.shape

    def pixel_yx(value: tuple[int, int]) -> tuple[int, int]:
        return (
            int(round(float(value[0]) * max(0, height - 1) / 1000.0)),
            int(round(float(value[1]) * max(0, width - 1) / 1000.0)),
        )

    bounded = np.zeros(binary.shape, dtype=bool)
    for raw_box in boxes_yxyx:
        y0, x0 = pixel_yx(raw_box[:2])
        y1, x1 = pixel_yx(raw_box[2:])
        pad_y = int(round(max(1, y1 - y0) * padding))
        pad_x = int(round(max(1, x1 - x0) * padding))
        y0 = max(0, y0 - pad_y)
        x0 = max(0, x0 - pad_x)
        y1 = min(height - 1, y1 + pad_y)
        x1 = min(width - 1, x1 + pad_x)
        bounded[y0 : y1 + 1, x0 : x1 + 1] = True
    candidate = binary & bounded
    finite = np.isfinite(depth) & (depth > 0.0)
    traversable = candidate & finite

    seeds: list[tuple[int, int]] = []
    for raw_point in positive_points_yx:
        y, x = pixel_yx(raw_point)
        if traversable[y, x]:
            seeds.append((y, x))
            continue
        y0, y1 = max(0, y - search_radius), min(height, y + search_radius + 1)
        x0, x1 = max(0, x - search_radius), min(width, x + search_radius + 1)
        local_y, local_x = np.nonzero(traversable[y0:y1, x0:x1])
        if local_y.size == 0:
            continue
        distance = (local_y + y0 - y) ** 2 + (local_x + x0 - x) ** 2
        nearest = int(np.argmin(distance))
        seeds.append((int(local_y[nearest] + y0), int(local_x[nearest] + x0)))

    connected = np.zeros(binary.shape, dtype=bool)
    for y, x in dict.fromkeys(seeds):
        flood_mask = np.ones((height + 2, width + 2), dtype=np.uint8)
        flood_mask[1:-1, 1:-1][traversable] = 0
        flags = 4 | cv2.FLOODFILL_MASK_ONLY | (2 << 8)
        cv2.floodFill(
            depth.copy(),
            flood_mask,
            (x, y),
            0.0,
            loDiff=threshold,
            upDiff=threshold,
            flags=flags,
        )
        connected |= flood_mask[1:-1, 1:-1] == 2

    # When registered depth is absent, keep the VLM-bounded 2D mask so the
    # VLM reviewer and tracker remain useful. It contributes no 3D spheres
    # until metric depth becomes available.
    depth_fallback = not np.any(traversable)
    output = candidate if depth_fallback else candidate & connected
    return np.ascontiguousarray(output), {
        "sam2_pixels": int(np.count_nonzero(binary)),
        "vlm_box_bounded_pixels": int(np.count_nonzero(candidate)),
        "depth_connected_pixels": int(np.count_nonzero(output)),
        "positive_seed_count": len(seeds),
        "registered_depth_fallback": depth_fallback,
    }


def partition_semantic_masks(
    *,
    policy: SceneSegmentationPolicy,
    declared_masks: dict[str, np.ndarray],
    arm_mask: np.ndarray,
    valid_depth_mask: np.ndarray,
    arm_dilation_pixels: int,
) -> MaskPartition:
    """Apply arm exclusion first, then explicit semantic-label precedence."""

    shape = np.asarray(valid_depth_mask).shape
    if len(shape) != 2:
        raise ValueError("valid_depth_mask must be two-dimensional")
    valid_depth = _mask(valid_depth_mask, shape, "valid_depth_mask")
    arm = _mask(arm_mask, shape, "arm_mask")
    dilated_arm = dilate_binary_mask(arm, arm_dilation_pixels)
    available = valid_depth & ~dilated_arm
    claimed = np.zeros(shape, dtype=bool)
    output: dict[str, np.ndarray] = {}
    object_types: dict[str, str] = {}

    priority = {"KEEP_OUT": 0, "WORK_OBJECT": 1, "PUSHABLE": 2}
    ordered = sorted(policy.objects, key=lambda value: priority[value.object_type])
    missing: list[str] = []
    for description in ordered:
        raw_mask = declared_masks.get(description.object_id)
        if raw_mask is None:
            missing.append(description.object_id)
            continue
        candidate = _mask(
            raw_mask,
            shape,
            f"declared mask {description.object_id}",
        )
        clean = candidate & available & ~claimed
        output[description.object_id] = np.ascontiguousarray(clean)
        object_types[description.object_id] = description.object_type
        claimed |= clean

    pushable = available & ~claimed
    return MaskPartition(
        object_masks=output,
        object_types=object_types,
        dilated_arm_mask=dilated_arm,
        pushable_mask=np.ascontiguousarray(pushable),
        diagnostics={
            "arm_mask_pixels": int(np.count_nonzero(arm)),
            "dilated_arm_mask_pixels": int(np.count_nonzero(dilated_arm)),
            "valid_depth_pixels": int(np.count_nonzero(valid_depth)),
            "declared_mask_pixels": {
                key: int(np.count_nonzero(value)) for key, value in output.items()
            },
            "unclaimed_pushable_pixels": int(np.count_nonzero(pushable)),
            "missing_declared_masks": missing,
            "precedence": ["ARM_EXCLUDED", "KEEP_OUT", "WORK_OBJECT", "PUSHABLE"],
        },
    )


def _rotation_xyzw(value: Any) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("rotation_xyzw must contain four finite values")
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("rotation_xyzw must have non-zero norm")
    x, y, z, w = quaternion / norm
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def project_masked_depth_to_frame(
    *,
    depth_m: np.ndarray,
    mask: np.ndarray,
    intrinsics: dict[str, Any],
    target_from_camera: dict[str, Any],
    pixel_stride: int = 2,
    minimum_depth_m: float = 0.05,
    maximum_depth_m: float = 5.0,
) -> np.ndarray:
    """Deproject aligned masked depth and transform it into the target frame."""

    depth = np.asarray(depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError("depth_m must be two-dimensional")
    selected = _mask(mask, depth.shape, "mask")
    stride = int(pixel_stride)
    if stride < 1 or stride > 64:
        raise ValueError("pixel_stride must be between 1 and 64")
    minimum_depth = float(minimum_depth_m)
    maximum_depth = float(maximum_depth_m)
    if (
        not math.isfinite(minimum_depth)
        or not math.isfinite(maximum_depth)
        or not 0.0 < minimum_depth < maximum_depth
    ):
        raise ValueError("depth bounds are invalid")
    valid = (
        selected
        & np.isfinite(depth)
        & (depth >= minimum_depth)
        & (depth <= maximum_depth)
    )
    sampled = np.zeros(depth.shape, dtype=bool)
    sampled[::stride, ::stride] = True
    ys, xs = np.nonzero(valid & sampled)
    if ys.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    fx = float(intrinsics.get("fx") or 0.0)
    fy = float(intrinsics.get("fy") or 0.0)
    cx = float(intrinsics.get("cx") or 0.0)
    cy = float(intrinsics.get("cy") or 0.0)
    if min(fx, fy) <= 0.0 or not all(
        math.isfinite(value) for value in (fx, fy, cx, cy)
    ):
        raise ValueError("camera intrinsics are invalid")
    z = depth[ys, xs]
    camera_points = np.column_stack(
        ((xs.astype(np.float64) - cx) * z / fx, (ys - cy) * z / fy, z)
    )
    translation = np.asarray(
        target_from_camera.get("translation_m"), dtype=np.float64
    )
    if translation.shape != (3,) or not np.all(np.isfinite(translation)):
        raise ValueError("target transform translation is invalid")
    rotation = _rotation_xyzw(target_from_camera.get("rotation_xyzw"))
    return camera_points @ rotation.T + translation
