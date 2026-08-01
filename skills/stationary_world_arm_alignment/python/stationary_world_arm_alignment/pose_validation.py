from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np


BOX_EDGES = (
    (0, 1),
    (0, 2),
    (0, 4),
    (1, 3),
    (1, 5),
    (2, 3),
    (2, 6),
    (3, 7),
    (4, 5),
    (4, 6),
    (5, 7),
    (6, 7),
)


@lru_cache(maxsize=4)
def load_model_vertices(
    workspace_root: str,
    model_id: str,
    model_registry: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    root = Path(workspace_root).resolve()
    if model_registry:
        registry_path = Path(model_registry)
        if not registry_path.is_absolute():
            registry_path = root / registry_path
        registry_path = registry_path.resolve()
    else:
        registry_path = (
            root
            / "providers"
            / "foundation_pose"
            / "defaults"
            / "rebot_b601_dm"
            / "models.json"
        )
    profile_root = registry_path.parent
    import json

    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    entry = next(model for model in registry["models"] if model["model_id"] == model_id)
    mesh_path = Path(entry["mesh_path"])
    if not mesh_path.is_absolute():
        mesh_path = profile_root / mesh_path
    scale = float(entry.get("scale_to_m", 1.0))
    vertices: list[list[float]] = []
    with mesh_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                fields = line.split()
                vertices.append([float(fields[1]), float(fields[2]), float(fields[3])])
    if not vertices:
        raise RuntimeError(f"FoundationPose mesh has no vertices: {mesh_path}")
    points = np.asarray(vertices, dtype=np.float64) * scale
    mesh_from_semantic = np.asarray(
        entry.get("mesh_from_semantic") or np.eye(4),
        dtype=np.float64,
    ).reshape(4, 4)
    return points, mesh_from_semantic


@lru_cache(maxsize=4)
def load_model_geometry(
    workspace_root: str,
    model_id: str,
    model_registry: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    points, mesh_from_semantic = load_model_vertices(
        workspace_root,
        model_id,
        model_registry,
    )
    return points.min(axis=0), points.max(axis=0), mesh_from_semantic


def _corners(minimum: np.ndarray, maximum: np.ndarray) -> np.ndarray:
    return np.asarray(
        [
            [x, y, z]
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        ],
        dtype=np.float64,
    )


def _project(points: np.ndarray, intrinsics: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    depth = points[:, 2]
    valid = (depth > 1e-5) & np.all(np.isfinite(points), axis=1)
    pixels = np.full((len(points), 2), np.nan, dtype=np.float64)
    pixels[valid, 0] = (
        float(intrinsics["fx"]) * points[valid, 0] / depth[valid]
        + float(intrinsics["cx"])
    )
    pixels[valid, 1] = (
        float(intrinsics["fy"]) * points[valid, 1] / depth[valid]
        + float(intrinsics["cy"])
    )
    return pixels, valid


def _pixel_tuple(pixel: np.ndarray) -> tuple[int, int]:
    clipped = np.clip(np.rint(pixel), -1_000_000, 1_000_000).astype(int)
    return int(clipped[0]), int(clipped[1])


def render_pose_overlay(
    rgb: np.ndarray,
    camera_from_semantic: np.ndarray,
    intrinsics: dict[str, Any],
    mesh_minimum_m: np.ndarray,
    mesh_maximum_m: np.ndarray,
    mesh_from_semantic: np.ndarray,
    *,
    axis_length_m: float,
    attempt: int,
) -> tuple[bytes, dict[str, Any]]:
    semantic_from_mesh = np.linalg.inv(mesh_from_semantic)
    mesh_corners = _corners(mesh_minimum_m, mesh_maximum_m)
    homogeneous = np.column_stack((mesh_corners, np.ones(8, dtype=np.float64)))
    semantic_corners = (semantic_from_mesh @ homogeneous.T).T[:, :3]
    camera_corners = (
        camera_from_semantic
        @ np.column_stack((semantic_corners, np.ones(8, dtype=np.float64))).T
    ).T[:, :3]
    box_pixels, box_valid = _project(camera_corners, intrinsics)

    axes_semantic = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [axis_length_m, 0.0, 0.0],
            [0.0, axis_length_m, 0.0],
            [0.0, 0.0, axis_length_m],
        ],
        dtype=np.float64,
    )
    camera_axes = (
        camera_from_semantic
        @ np.column_stack((axes_semantic, np.ones(4, dtype=np.float64))).T
    ).T[:, :3]
    axis_pixels, axis_valid = _project(camera_axes, intrinsics)

    canvas = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    box_color = (0, 235, 255)
    for first, second in BOX_EDGES:
        if box_valid[first] and box_valid[second]:
            cv2.line(
                canvas,
                _pixel_tuple(box_pixels[first]),
                _pixel_tuple(box_pixels[second]),
                box_color,
                3,
                cv2.LINE_AA,
            )
    origin_ok = bool(axis_valid[0])
    axis_colors = ((0, 0, 255), (0, 220, 0), (255, 90, 30))
    for index, (color, label) in enumerate(zip(axis_colors, ("X", "Y", "Z")), start=1):
        if origin_ok and axis_valid[index]:
            origin = _pixel_tuple(axis_pixels[0])
            endpoint = _pixel_tuple(axis_pixels[index])
            cv2.arrowedLine(canvas, origin, endpoint, color, 4, cv2.LINE_AA, tipLength=0.18)
            cv2.putText(
                canvas,
                label,
                endpoint,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                color,
                2,
                cv2.LINE_AA,
            )
    cv2.putText(
        canvas,
        f"FoundationPose base attempt {attempt}: projected 3D box + XYZ axes",
        (24, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    height, width = rgb.shape[:2]
    projected_box_xyxy_px: list[float] | None = None
    positive_pixels = box_pixels[box_valid]
    if len(positive_pixels):
        clipped_x = np.clip(positive_pixels[:, 0], 0.0, max(0, width - 1))
        clipped_y = np.clip(positive_pixels[:, 1], 0.0, max(0, height - 1))
        projected_box_xyxy_px = [
            float(np.min(clipped_x)),
            float(np.min(clipped_y)),
            float(np.max(clipped_x)),
            float(np.max(clipped_y)),
        ]
    visible = [
        bool(
            box_valid[index]
            and 0 <= box_pixels[index, 0] < width
            and 0 <= box_pixels[index, 1] < height
        )
        for index in range(8)
    ]
    success, encoded = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 94])
    if not success:
        raise RuntimeError("failed to encode FoundationPose validation overlay")
    return encoded.tobytes(), {
        "attempt": attempt,
        "positive_depth_corner_count": int(np.count_nonzero(box_valid)),
        "visible_corner_count": int(sum(visible)),
        "axis_origin_visible": bool(
            origin_ok
            and 0 <= axis_pixels[0, 0] < width
            and 0 <= axis_pixels[0, 1] < height
        ),
        "projected_box_xyxy_px": projected_box_xyxy_px,
        "image_size_px": [width, height],
    }


def projected_visual_scale_review(
    projection: dict[str, Any],
    visual_box_yxyx_1000: list[int],
    image_shape: tuple[int, ...],
    *,
    maximum_mismatch_fraction: float = 0.25,
) -> dict[str, Any]:
    """Compare projected CAD and visual boxes without asking a VLM to measure."""
    limit = float(maximum_mismatch_fraction)
    if not 0.0 < limit < 1.0:
        raise ValueError("maximum_mismatch_fraction must be in (0, 1)")
    if len(image_shape) < 2:
        raise ValueError("image_shape must contain height and width")
    height, width = int(image_shape[0]), int(image_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("image dimensions must be positive")
    if (
        not isinstance(visual_box_yxyx_1000, list)
        or len(visual_box_yxyx_1000) != 4
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in visual_box_yxyx_1000
        )
    ):
        raise ValueError("visual box must be integer [y0,x0,y1,x1] in 0..1000")
    y0, x0, y1, x1 = visual_box_yxyx_1000
    visual_width = (x1 - x0) * width / 1000.0
    visual_height = (y1 - y0) * height / 1000.0
    projected = projection.get("projected_box_xyxy_px")
    projected_width = 0.0
    projected_height = 0.0
    if isinstance(projected, list) and len(projected) == 4:
        px0, py0, px1, py1 = (float(item) for item in projected)
        projected_width = px1 - px0
        projected_height = py1 - py0
    visual_area = visual_width * visual_height
    projected_area = projected_width * projected_height
    available = bool(visual_area > 0.0 and projected_area > 0.0)
    scale_ratio = (
        float(np.sqrt(projected_area / visual_area))
        if available
        else None
    )
    mismatch = (
        abs(scale_ratio - 1.0) if scale_ratio is not None else None
    )
    within = bool(mismatch is not None and mismatch <= limit + 1e-12)
    if not available:
        warning = (
            "Projected or visual base box is degenerate; size could not be "
            "compared."
        )
    elif within:
        warning = None
    else:
        warning = (
            f"Projected CAD linear scale ratio {scale_ratio:.3f} is outside "
            f"the inclusive {1.0 - limit:.2f}..{1.0 + limit:.2f} band."
        )
    return {
        "method": "PROJECTED_CAD_VERSUS_TIGHT_VISUAL_BOX_LINEAR_SCALE",
        "available": available,
        "visual_box_yxyx_1000": list(visual_box_yxyx_1000),
        "projected_box_xyxy_px": projected,
        "visual_size_px": [visual_width, visual_height],
        "projected_size_px": [projected_width, projected_height],
        "width_ratio": (
            projected_width / visual_width if visual_width > 0.0 else None
        ),
        "height_ratio": (
            projected_height / visual_height if visual_height > 0.0 else None
        ),
        "area_ratio": (
            projected_area / visual_area if visual_area > 0.0 else None
        ),
        "equivalent_linear_scale_ratio": scale_ratio,
        "mismatch_fraction": mismatch,
        "maximum_mismatch_fraction": limit,
        "within_tolerance": within,
        "warning": warning,
    }


def select_best_pose_validation(
    validations: list[dict[str, Any]],
) -> int:
    if not validations:
        raise ValueError("at least one pose validation is required")
    def quality(record: dict[str, Any]) -> tuple[int, float, int]:
        scale = record.get("scale_review") or {}
        projection = record.get("projection") or {}
        mismatch = scale.get("mismatch_fraction")
        return (
            0 if isinstance(mismatch, (int, float)) else 1,
            float(mismatch) if isinstance(mismatch, (int, float)) else float("inf"),
            -int(projection.get("visible_corner_count") or 0),
        )

    return min(
        range(len(validations)),
        key=lambda index: quality(validations[index]),
    )
