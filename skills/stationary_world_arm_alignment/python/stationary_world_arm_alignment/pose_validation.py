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
def load_model_geometry(
    workspace_root: str,
    model_id: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    profile_root = (
        Path(workspace_root)
        / "providers"
        / "foundation_pose"
        / "defaults"
        / "rebot_b601_dm"
    )
    import json

    registry = json.loads((profile_root / "models.json").read_text(encoding="utf-8"))
    entry = next(model for model in registry["models"] if model["model_id"] == model_id)
    mesh_path = profile_root / entry["mesh_path"]
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
    }


def pose_verdict_accepted(verdict: dict[str, Any], minimum_confidence: float) -> bool:
    return bool(
        verdict.get("pose_reasonable")
        and float(verdict.get("confidence", 0.0)) >= minimum_confidence
        and verdict.get("box_fit") != "BAD"
        and verdict.get("orientation_fit") != "BAD"
    )


def pose_verdict_best_of_two_acceptable(
    verdict: dict[str, Any],
    fallback_minimum_confidence: float,
) -> bool:
    return bool(
        verdict.get("pose_reasonable")
        and float(verdict.get("confidence", 0.0))
        >= fallback_minimum_confidence
        and verdict.get("box_fit") in {"GOOD", "ACCEPTABLE"}
        and verdict.get("orientation_fit") in {"GOOD", "ACCEPTABLE"}
    )


def select_best_pose_validation(
    validations: list[dict[str, Any]],
) -> int:
    if not validations:
        raise ValueError("at least one pose validation is required")
    fit_rank = {"BAD": 0, "ACCEPTABLE": 1, "GOOD": 2}

    def quality(record: dict[str, Any]) -> tuple[int, int, int, float, int]:
        verdict = record.get("verdict") or {}
        projection = record.get("projection") or {}
        return (
            int(bool(verdict.get("pose_reasonable"))),
            int(fit_rank.get(str(verdict.get("box_fit")), -1)),
            int(fit_rank.get(str(verdict.get("orientation_fit")), -1)),
            float(verdict.get("confidence") or 0.0),
            int(projection.get("visible_corner_count") or 0),
        )

    return max(
        range(len(validations)),
        key=lambda index: quality(validations[index]),
    )
