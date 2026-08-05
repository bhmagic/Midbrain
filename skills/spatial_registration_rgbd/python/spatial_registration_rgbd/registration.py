from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class DepthSelection:
    pixel_yx: tuple[int, int]
    depth_m: float
    policy: str
    requested_pixel_yx: tuple[int, int]
    search_radius_px: int
    valid_samples: int
    patch_samples: int
    median_m: float
    p10_m: float
    p90_m: float
    median_absolute_deviation_m: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def map_pixel_between_grids(
    pixel_yx: tuple[float, float],
    source_grid: tuple[int, int],
    target_grid: tuple[int, int],
) -> tuple[float, float]:
    """Map pixel centers between independent grids using normalized image space."""

    source_height, source_width = (int(source_grid[0]), int(source_grid[1]))
    target_height, target_width = (int(target_grid[0]), int(target_grid[1]))
    if min(source_height, source_width, target_height, target_width) <= 0:
        raise ValueError("source and target grids must be positive")
    y, x = float(pixel_yx[0]), float(pixel_yx[1])
    mapped_y = (y + 0.5) * target_height / source_height - 0.5
    mapped_x = (x + 0.5) * target_width / source_width - 0.5
    return mapped_y, mapped_x


def normalized_1000_point_to_pixel(
    point_yx: list[int] | tuple[int, int],
    target_grid: tuple[int, int],
) -> tuple[int, int]:
    """Map a VLM normalized 0..1000 point onto a native pixel grid."""

    height, width = (int(target_grid[0]), int(target_grid[1]))
    if height <= 0 or width <= 0:
        raise ValueError("target_grid must be positive")
    if len(point_yx) != 2:
        raise ValueError("normalized point must contain [y, x]")
    y, x = (int(point_yx[0]), int(point_yx[1]))
    if not 0 <= y <= 1000 or not 0 <= x <= 1000:
        raise ValueError("normalized point coordinates must be within 0..1000")
    return (
        int(round(y * (height - 1) / 1000.0)),
        int(round(x * (width - 1) / 1000.0)),
    )


def normalized_1000_box_to_pixels(
    box_yxyx: list[int] | tuple[int, int, int, int],
    target_grid: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Map a normalized box to half-open native pixel bounds."""

    height, width = (int(target_grid[0]), int(target_grid[1]))
    if height <= 0 or width <= 0:
        raise ValueError("target_grid must be positive")
    if len(box_yxyx) != 4:
        raise ValueError("normalized box must contain [y0, x0, y1, x1]")
    y0, x0, y1, x1 = (int(value) for value in box_yxyx)
    if any(not 0 <= value <= 1000 for value in (y0, x0, y1, x1)):
        raise ValueError("normalized box coordinates must be within 0..1000")
    return (
        max(0, min(height - 1, int(np.floor(y0 * height / 1000.0)))),
        max(0, min(width - 1, int(np.floor(x0 * width / 1000.0)))),
        max(1, min(height, int(np.ceil(y1 * height / 1000.0)))),
        max(1, min(width, int(np.ceil(x1 * width / 1000.0)))),
    )


def select_depth_sample(
    depth_m: np.ndarray,
    pixel_yx: tuple[float, float],
    *,
    search_radius_px: int = 3,
    policy: str = "ROBUST_MEDIAN",
    minimum_depth_m: float = 0.05,
    maximum_depth_m: float = 20.0,
    valid_region: dict[str, Any] | None = None,
) -> DepthSelection:
    values = np.asarray(depth_m, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("depth_m must be a two-dimensional array")
    radius = max(0, int(search_radius_px))
    requested_y = int(round(float(pixel_yx[0])))
    requested_x = int(round(float(pixel_yx[1])))
    height, width = values.shape
    requested_y = int(np.clip(requested_y, 0, height - 1))
    requested_x = int(np.clip(requested_x, 0, width - 1))
    y0, y1 = max(0, requested_y - radius), min(height, requested_y + radius + 1)
    x0, x1 = max(0, requested_x - radius), min(width, requested_x + radius + 1)
    patch = values[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    valid = (
        np.isfinite(patch)
        & (patch >= float(minimum_depth_m))
        & (patch <= float(maximum_depth_m))
    )
    if valid_region:
        region_x = int(valid_region.get("x") or 0)
        region_y = int(valid_region.get("y") or 0)
        region_width = int(valid_region.get("width") or width)
        region_height = int(valid_region.get("height") or height)
        valid &= (
            (xx >= region_x)
            & (xx < region_x + region_width)
            & (yy >= region_y)
            & (yy < region_y + region_height)
        )
    if not np.any(valid):
        raise RuntimeError("the requested RGB-D point has no valid nearby depth")

    valid_depth = patch[valid]
    valid_y = yy[valid]
    valid_x = xx[valid]
    normalized_policy = str(policy).upper()
    distances_sq = (valid_y - requested_y) ** 2 + (valid_x - requested_x) ** 2
    if normalized_policy == "CLOSEST_TO_CAMERA":
        chosen_index = int(
            np.lexsort((distances_sq, valid_depth))[0]
        )
    elif normalized_policy == "NEAREST_VALID_PIXEL":
        chosen_index = int(
            np.lexsort((valid_depth, distances_sq))[0]
        )
    elif normalized_policy == "ROBUST_MEDIAN":
        median = float(np.median(valid_depth))
        residual = np.abs(valid_depth - median)
        chosen_index = int(
            np.lexsort((distances_sq, residual))[0]
        )
    else:
        raise ValueError(f"unsupported depth selection policy {policy}")

    median = float(np.median(valid_depth))
    return DepthSelection(
        pixel_yx=(int(valid_y[chosen_index]), int(valid_x[chosen_index])),
        depth_m=float(valid_depth[chosen_index]),
        policy=normalized_policy,
        requested_pixel_yx=(requested_y, requested_x),
        search_radius_px=radius,
        valid_samples=int(valid_depth.size),
        patch_samples=int(patch.size),
        median_m=median,
        p10_m=float(np.percentile(valid_depth, 10)),
        p90_m=float(np.percentile(valid_depth, 90)),
        median_absolute_deviation_m=float(
            np.median(np.abs(valid_depth - median))
        ),
    )


def deproject_pixel(
    pixel_yx: tuple[int, int],
    depth_m: float,
    intrinsics: dict[str, Any],
) -> np.ndarray:
    y, x = pixel_yx
    fx = float(intrinsics["fx"])
    fy = float(intrinsics["fy"])
    cx = float(intrinsics["cx"])
    cy = float(intrinsics["cy"])
    if fx <= 0.0 or fy <= 0.0:
        raise ValueError("camera focal lengths must be positive")
    depth = float(depth_m)
    if not np.isfinite(depth) or depth <= 0.0:
        raise ValueError("depth_m must be positive and finite")
    return np.asarray(
        [
            (float(x) - cx) * depth / fx,
            (float(y) - cy) * depth / fy,
            depth,
        ],
        dtype=np.float64,
    )


def transform_point(transform_target_from_source: np.ndarray, point: np.ndarray) -> np.ndarray:
    matrix = np.asarray(transform_target_from_source, dtype=np.float64)
    value = np.asarray(point, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("transform must be a finite 4x4 matrix")
    if value.shape != (3,) or not np.all(np.isfinite(value)):
        raise ValueError("point must contain three finite values")
    return (matrix @ np.append(value, 1.0))[:3]


def register_rgbd_point(
    *,
    rgb_pixel_yx: tuple[float, float],
    rgb_grid: tuple[int, int],
    registered_depth_m: np.ndarray,
    registered_depth_grid: tuple[int, int],
    intrinsics: dict[str, Any],
    target_from_camera: np.ndarray,
    observed_at_us: int,
    source_frame: str,
    target_frame: str,
    calibration_revision: str | None,
    route_provenance: dict[str, Any],
    depth_policy: str = "ROBUST_MEDIAN",
    search_radius_px: int = 3,
    valid_region: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mapped = map_pixel_between_grids(
        rgb_pixel_yx,
        rgb_grid,
        registered_depth_grid,
    )
    selection = select_depth_sample(
        registered_depth_m,
        mapped,
        search_radius_px=search_radius_px,
        policy=depth_policy,
        valid_region=valid_region,
    )
    camera_system_xyz_m = deproject_pixel(
        selection.pixel_yx,
        selection.depth_m,
        intrinsics,
    )
    target_point = transform_point(
        target_from_camera,
        camera_system_xyz_m,
    )
    return {
        "schema": "physical_agent.spatial_registration_rgbd",
        "schema_version": 1,
        "observed_at_us": int(observed_at_us),
        "source_frame": str(source_frame),
        "target_frame": str(target_frame),
        "rgb_pixel_yx": [float(rgb_pixel_yx[0]), float(rgb_pixel_yx[1])],
        "registered_depth_pixel_yx": list(selection.pixel_yx),
        "camera_system_point_m": {
            "camera_system_x": float(camera_system_xyz_m[0]),
            "camera_system_y": float(camera_system_xyz_m[1]),
            "camera_system_z": float(camera_system_xyz_m[2]),
        },
        "target_point_m": target_point.tolist(),
        "depth_selection": selection.as_dict(),
        "calibration_revision": calibration_revision,
        "data_route": dict(route_provenance),
    }
