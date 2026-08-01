from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .math3d import normalized_yx_to_pixel


@dataclass(frozen=True)
class Plane:
    origin_m: np.ndarray
    normal: np.ndarray
    axis_u: np.ndarray
    axis_v: np.ndarray
    rmse_m: float

    def project(self, points_m: np.ndarray) -> np.ndarray:
        delta = np.asarray(points_m, dtype=np.float64).reshape(-1, 3) - self.origin_m
        return np.column_stack([delta @ self.axis_u, delta @ self.axis_v])

    def lift(self, points_uv_m: np.ndarray, offset_m: float = 0.0) -> np.ndarray:
        values = np.asarray(points_uv_m, dtype=np.float64).reshape(-1, 2)
        return (
            self.origin_m
            + values[:, :1] * self.axis_u
            + values[:, 1:] * self.axis_v
            + float(offset_m) * self.normal
        )


def polygon_mask(shape: tuple[int, int] | tuple[int, int, int], polygon_yx_1000: list[list[int]]) -> np.ndarray:
    points = [
        [normalized_yx_to_pixel(point, shape)[1], normalized_yx_to_pixel(point, shape)[0]]
        for point in polygon_yx_1000
    ]
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [np.asarray(points, dtype=np.int32)], 255)
    return mask


def fit_board_plane(
    depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    board_mask: np.ndarray,
    *,
    stride_px: int,
    minimum_points: int,
) -> tuple[Plane, float]:
    height, width = depth_m.shape
    yy, xx = np.mgrid[0:height:stride_px, 0:width:stride_px]
    depth = depth_m[0:height:stride_px, 0:width:stride_px]
    selected = board_mask[0:height:stride_px, 0:width:stride_px] > 0
    valid = selected & np.isfinite(depth) & (depth > 0.05)
    selected_count = int(np.count_nonzero(selected))
    valid_count = int(np.count_nonzero(valid))
    valid_fraction = float(valid_count / max(selected_count, 1))
    if valid_count < int(minimum_points):
        raise RuntimeError(
            f"board plane has {valid_count} valid points; {minimum_points} are required"
        )
    z = depth[valid].astype(np.float64)
    x = (xx[valid].astype(np.float64) - float(intrinsics["cx"])) * z / float(intrinsics["fx"])
    y = (yy[valid].astype(np.float64) - float(intrinsics["cy"])) * z / float(intrinsics["fy"])
    points = np.column_stack([x, y, z])

    # Use two robust trimming passes so vegetable pixels inside the board polygon
    # do not pull the fitted board plane upward.
    working = points
    for _ in range(2):
        center = np.median(working, axis=0)
        _, _, vh = np.linalg.svd(working - center, full_matrices=False)
        normal = vh[-1]
        residual = np.abs((points - center) @ normal)
        cutoff = max(0.002, float(np.percentile(residual, 55)))
        trimmed = points[residual <= cutoff]
        if trimmed.shape[0] < minimum_points:
            break
        working = trimmed

    origin = np.mean(working, axis=0)
    _, _, vh = np.linalg.svd(working - origin, full_matrices=False)
    normal = vh[-1]
    if normal[2] > 0:
        normal = -normal
    normal = normal / np.linalg.norm(normal)
    camera_x = np.asarray([1.0, 0.0, 0.0])
    axis_u = camera_x - normal * float(camera_x @ normal)
    if float(np.linalg.norm(axis_u)) < 1e-6:
        axis_u = np.asarray([0.0, 1.0, 0.0])
        axis_u -= normal * float(axis_u @ normal)
    axis_u /= np.linalg.norm(axis_u)
    axis_v = np.cross(normal, axis_u)
    axis_v /= np.linalg.norm(axis_v)
    rmse = float(np.sqrt(np.mean(((working - origin) @ normal) ** 2)))
    return Plane(origin, normal, axis_u, axis_v, rmse), valid_fraction


def refine_object_mask_above_plane(
    depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    board_mask: np.ndarray,
    plane: Plane,
    vlm_mask: np.ndarray,
    *,
    minimum_height_m: float,
    maximum_height_m: float,
    minimum_component_pixels: int,
    minimum_vlm_overlap_pixels: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    valid = (
        (board_mask > 0)
        & np.isfinite(depth_m)
        & (depth_m > 0.05)
    )
    yy, xx = np.nonzero(valid)
    if yy.size < minimum_component_pixels:
        raise RuntimeError("board has too few valid depth pixels for object refinement")
    z = depth_m[yy, xx].astype(np.float64)
    points = np.column_stack(
        [
            (xx.astype(np.float64) - float(intrinsics["cx"]))
            * z
            / float(intrinsics["fx"]),
            (yy.astype(np.float64) - float(intrinsics["cy"]))
            * z
            / float(intrinsics["fy"]),
            z,
        ]
    )
    heights = (points - plane.origin_m) @ plane.normal
    selected = (
        (heights >= float(minimum_height_m))
        & (heights <= float(maximum_height_m))
    )
    candidate = np.zeros(depth_m.shape, dtype=np.uint8)
    candidate[yy[selected], xx[selected]] = 255
    candidate = cv2.morphologyEx(
        candidate,
        cv2.MORPH_OPEN,
        np.ones((3, 3), np.uint8),
    )
    candidate = cv2.morphologyEx(
        candidate,
        cv2.MORPH_CLOSE,
        np.ones((9, 9), np.uint8),
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate)
    choices: list[tuple[int, int, int]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < int(minimum_component_pixels):
            continue
        overlap = int(np.count_nonzero((labels == label) & (vlm_mask > 0)))
        choices.append((overlap, area, label))
    if not choices:
        raise RuntimeError("RGB-D found no raised object component on the cutting board")
    overlap, area, selected_label = max(choices)
    if overlap < int(minimum_vlm_overlap_pixels):
        raise RuntimeError(
            "RGB-D raised objects do not overlap the VLM vegetable localization"
        )
    output = np.zeros(depth_m.shape, dtype=np.uint8)
    output[labels == selected_label] = 255
    vlm_iou_union = int(np.count_nonzero((output > 0) | (vlm_mask > 0)))
    vlm_iou = float(
        np.count_nonzero((output > 0) & (vlm_mask > 0))
        / max(vlm_iou_union, 1)
    )
    component_heights = heights[
        selected & (labels[yy, xx] == selected_label)
    ]
    return output, {
        "source": "RGBD_HEIGHT_ABOVE_BOARD_PLANE",
        "component_pixels": area,
        "vlm_overlap_pixels": overlap,
        "vlm_mask_iou": vlm_iou,
        "median_height_mm": float(np.median(component_heights) * 1000.0),
        "maximum_height_mm": float(np.max(component_heights) * 1000.0),
    }


def mask_to_normalized_polygon(
    mask: np.ndarray,
    *,
    maximum_points: int = 24,
) -> list[list[int]]:
    contours, _ = cv2.findContours(
        (mask > 0).astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if not contours:
        raise RuntimeError("object mask has no contour")
    contour = max(contours, key=cv2.contourArea)
    perimeter = float(cv2.arcLength(contour, True))
    polygon = cv2.approxPolyDP(contour, max(1.0, perimeter * 0.008), True)
    points = polygon.reshape(-1, 2)
    if points.shape[0] < 4:
        points = cv2.convexHull(contour).reshape(-1, 2)
    if points.shape[0] > int(maximum_points):
        indices = np.linspace(
            0,
            points.shape[0] - 1,
            int(maximum_points),
            dtype=int,
        )
        points = points[indices]
    if points.shape[0] < 4:
        raise RuntimeError("object mask contour has fewer than four points")
    height, width = mask.shape
    return [
        [
            int(round(float(y) * 1000.0 / max(height - 1, 1))),
            int(round(float(x) * 1000.0 / max(width - 1, 1))),
        ]
        for x, y in points
    ]


def polygon_pixels_to_plane(
    polygon_yx_1000: list[list[int]],
    shape: tuple[int, ...],
    intrinsics: dict[str, Any],
    plane: Plane,
) -> np.ndarray:
    output: list[np.ndarray] = []
    for point in polygon_yx_1000:
        y, x = normalized_yx_to_pixel(point, shape)
        ray = np.asarray(
            [
                (float(x) - float(intrinsics["cx"])) / float(intrinsics["fx"]),
                (float(y) - float(intrinsics["cy"])) / float(intrinsics["fy"]),
                1.0,
            ],
            dtype=np.float64,
        )
        denominator = float(plane.normal @ ray)
        if abs(denominator) < 1e-8:
            raise RuntimeError("polygon ray is parallel to the board plane")
        distance = float(plane.normal @ plane.origin_m) / denominator
        if distance <= 0:
            raise RuntimeError("polygon ray intersects the board behind the camera")
        point_m = ray * distance
        output.append(plane.project(point_m)[0])
    return np.asarray(output, dtype=np.float64)


def principal_axis(polygon_uv_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(polygon_uv_m, dtype=np.float64)
    center = np.mean(points, axis=0)
    covariance = np.cov((points - center).T)
    values, vectors = np.linalg.eigh(covariance)
    axis = vectors[:, int(np.argmax(values))]
    if axis[0] < 0 or (abs(axis[0]) < 1e-12 and axis[1] < 0):
        axis = -axis
    return center, axis / np.linalg.norm(axis)


def _cross2(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def line_polygon_intersections(
    point_uv: np.ndarray,
    direction_uv: np.ndarray,
    polygon_uv_m: np.ndarray,
) -> list[float]:
    polygon = np.asarray(polygon_uv_m, dtype=np.float64)
    direction = np.asarray(direction_uv, dtype=np.float64)
    values: list[float] = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        edge = end - start
        denominator = _cross2(direction, edge)
        if abs(denominator) < 1e-10:
            continue
        delta = start - point_uv
        along_line = _cross2(delta, edge) / denominator
        along_edge = _cross2(delta, direction) / denominator
        if -1e-8 <= along_edge <= 1.0 + 1e-8:
            values.append(float(along_line))
    values.sort()
    unique: list[float] = []
    for value in values:
        if not unique or abs(value - unique[-1]) > 1e-7:
            unique.append(value)
    return unique


def point_in_polygon(point_uv: np.ndarray, polygon_uv_m: np.ndarray) -> bool:
    polygon = np.asarray(polygon_uv_m, dtype=np.float64)
    x, y = float(point_uv[0]), float(point_uv[1])
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x0, y0 = float(previous[0]), float(previous[1])
        x1, y1 = float(current[0]), float(current[1])
        crosses = (y0 > y) != (y1 > y)
        if crosses:
            intersection = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x < intersection:
                inside = not inside
        previous = current
    return inside


def plan_cross_cuts(
    vegetable_uv_m: np.ndarray,
    board_uv_m: np.ndarray,
    *,
    spacing_m: float,
    vegetable_end_margin_m: float,
    board_entry_exit_margin_m: float,
    maximum_cut_count: int,
) -> dict[str, Any]:
    vegetable = np.asarray(vegetable_uv_m, dtype=np.float64)
    board = np.asarray(board_uv_m, dtype=np.float64)
    if vegetable.shape[0] < 3 or board.shape[0] < 3:
        raise ValueError("board and vegetable polygons require at least three points")
    if spacing_m <= 0:
        raise ValueError("spacing_m must be positive")

    center, axis = principal_axis(vegetable)
    cross_axis = np.asarray([-axis[1], axis[0]], dtype=np.float64)
    longitudinal = (vegetable - center) @ axis
    start = float(np.min(longitudinal)) + vegetable_end_margin_m
    stop = float(np.max(longitudinal)) - vegetable_end_margin_m
    if start > stop:
        stations = np.asarray([(float(np.min(longitudinal)) + float(np.max(longitudinal))) / 2.0])
    else:
        count = max(1, int(np.floor((stop - start) / spacing_m)) + 1)
        stations = start + np.arange(count, dtype=np.float64) * spacing_m
    stations = stations[: int(maximum_cut_count)]

    cuts: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for station_index, station in enumerate(stations):
        station_center = center + axis * station
        vegetable_hits = line_polygon_intersections(station_center, cross_axis, vegetable)
        board_hits = line_polygon_intersections(station_center, cross_axis, board)
        if len(vegetable_hits) < 2 or len(board_hits) < 2:
            rejected.append({"index": station_index, "reason": "polygon intersection unavailable"})
            continue
        vegetable_low, vegetable_high = min(vegetable_hits), max(vegetable_hits)
        board_low, board_high = min(board_hits), max(board_hits)
        entry_t = max(board_low, vegetable_low - board_entry_exit_margin_m)
        exit_t = min(board_high, vegetable_high + board_entry_exit_margin_m)
        if entry_t >= vegetable_low or exit_t <= vegetable_high:
            rejected.append({"index": station_index, "reason": "board margin is insufficient"})
            continue
        entry = station_center + cross_axis * entry_t
        exit_point = station_center + cross_axis * exit_t
        if not point_in_polygon(entry, board) or not point_in_polygon(exit_point, board):
            rejected.append({"index": station_index, "reason": "cut endpoint is outside board"})
            continue
        cuts.append(
            {
                "index": len(cuts),
                "station_longitudinal_m": float(station),
                "center_uv_m": station_center.tolist(),
                "entry_uv_m": entry.tolist(),
                "exit_uv_m": exit_point.tolist(),
                "vegetable_width_m": float(vegetable_high - vegetable_low),
                "path_length_m": float(exit_t - entry_t),
            }
        )
    return {
        "vegetable_center_uv_m": center.tolist(),
        "vegetable_axis_uv": axis.tolist(),
        "cut_cross_axis_uv": cross_axis.tolist(),
        "cuts": cuts,
        "rejected": rejected,
    }


def plan_cut_points_on_line_3d(
    left_board_point_m: np.ndarray,
    right_board_point_m: np.ndarray,
    *,
    spacing_m: float,
    maximum_cut_count: int,
) -> dict[str, Any]:
    """Interpolate cut centers on the 3D line between two board-depth points."""
    left = np.asarray(left_board_point_m, dtype=np.float64)
    right = np.asarray(right_board_point_m, dtype=np.float64)
    if (
        left.shape != (3,)
        or right.shape != (3,)
        or not np.all(np.isfinite(left))
        or not np.all(np.isfinite(right))
    ):
        raise ValueError("cutting-line endpoints must be finite 3D points")
    if spacing_m <= 0.0:
        raise ValueError("spacing_m must be positive")
    if maximum_cut_count < 1:
        raise ValueError("maximum_cut_count must be positive")
    delta = right - left
    length_m = float(np.linalg.norm(delta))
    if length_m <= 1e-6:
        raise ValueError("cutting-line endpoints are degenerate")
    axis = delta / length_m
    distances = np.arange(spacing_m, length_m, spacing_m)
    if not distances.size:
        distances = np.asarray([0.5 * length_m], dtype=np.float64)
    distances = distances[: int(maximum_cut_count)]
    points = left[None, :] + distances[:, None] * axis[None, :]
    return {
        "source": "TWO_RGBD_BOARD_POINTS_STRAIGHT_LINE",
        "left_board_point_m": left.tolist(),
        "right_board_point_m": right.tolist(),
        "line_length_m": length_m,
        "axis": axis.tolist(),
        "spacing_m": float(spacing_m),
        "cuts": [
            {
                "index": index,
                "distance_from_left_m": float(distance),
                "center_m": point.tolist(),
            }
            for index, (distance, point) in enumerate(
                zip(distances, points, strict=True)
            )
        ],
        "rejected": [],
    }
