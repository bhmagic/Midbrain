from __future__ import annotations

from functools import lru_cache
import math
from typing import Any

import numpy as np


ANGULAR_ROI_SCOPE = "HAND_ANGULAR_4PI"
ANGULAR_PROFILE_ID = "SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1"
SPATIAL_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
_GOLDEN_RATIO = (1.0 + math.sqrt(5.0)) / 2.0
_TAU = 2.0 * math.pi


def _direction_count(value: int) -> int:
    count = int(value)
    if not 128 <= count <= 65_536:
        raise ValueError("angular_direction_count must be in [128, 65536]")
    return count


@lru_cache(maxsize=8)
def spherical_fibonacci_directions(direction_count: int) -> np.ndarray:
    """Return deterministic near-uniform directions without polar crowding."""

    count = _direction_count(direction_count)
    indices = np.arange(count, dtype=np.float64)
    azimuth = _TAU * np.mod(indices * (_GOLDEN_RATIO - 1.0), 1.0)
    z = 1.0 - (2.0 * indices + 1.0) / float(count)
    radial = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    directions = np.column_stack(
        (np.cos(azimuth) * radial, np.sin(azimuth) * radial, z)
    )
    directions.setflags(write=False)
    return directions


def nearest_spherical_fibonacci_indices(
    vectors: np.ndarray,
    direction_count: int,
) -> np.ndarray:
    """Map vectors to exact nearest Fibonacci samples in constant time per vector.

    This is the vectorized form of the four-candidate inverse mapping from
    Keinert et al., "Spherical Fibonacci Mapping" (2015).
    """

    count = _direction_count(direction_count)
    values = np.asarray(vectors, dtype=np.float64)
    if values.size == 0:
        return np.empty((0,), dtype=np.int64)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("vectors must have shape Nx3")
    if not np.all(np.isfinite(values)):
        raise ValueError("vectors must contain only finite values")
    norms = np.linalg.norm(values, axis=1)
    if np.any(norms <= 1e-12):
        raise ValueError("vectors must be non-zero")
    unit = values / norms[:, None]

    z = np.clip(unit[:, 2], -1.0, 1.0)
    argument = (
        float(count)
        * math.pi
        * math.sqrt(5.0)
        * np.maximum(0.0, 1.0 - z * z)
    )
    logarithm = np.log(np.maximum(argument, np.finfo(np.float64).tiny))
    order = np.maximum(
        2,
        np.floor(logarithm / math.log(_GOLDEN_RATIO + 1.0)).astype(np.int64),
    )
    fibonacci_approximation = np.power(_GOLDEN_RATIO, order) / math.sqrt(5.0)
    fibonacci = np.rint(
        np.column_stack(
            (fibonacci_approximation, fibonacci_approximation * _GOLDEN_RATIO)
        )
    )
    height_basis = 2.0 * fibonacci / float(count)
    azimuth_basis = (
        np.mod((fibonacci + 1.0) * _GOLDEN_RATIO, 1.0)
        - (_GOLDEN_RATIO - 1.0)
    ) * _TAU
    determinant = (
        height_basis[:, 1] * azimuth_basis[:, 0]
        - height_basis[:, 0] * azimuth_basis[:, 1]
    )
    if np.any(np.abs(determinant) <= 1e-15):
        raise RuntimeError("spherical Fibonacci inverse basis is singular")

    azimuth = np.arctan2(unit[:, 1], unit[:, 0])
    shifted_height = z - 1.0 + 1.0 / float(count)
    lattice_cell = np.floor(
        np.column_stack(
            (
                (
                    height_basis[:, 1] * azimuth
                    + azimuth_basis[:, 1] * shifted_height
                )
                / determinant,
                (
                    -height_basis[:, 0] * azimuth
                    - azimuth_basis[:, 0] * shifted_height
                )
                / determinant,
            )
        )
    )

    directions = spherical_fibonacci_directions(count)
    best_indices = np.zeros(values.shape[0], dtype=np.int64)
    best_squared_distance = np.full(values.shape[0], np.inf, dtype=np.float64)
    for offset in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)):
        candidate_indices = np.clip(
            np.rint(
                np.sum(
                    fibonacci
                    * (lattice_cell + np.asarray(offset, dtype=np.float64)),
                    axis=1,
                )
            ).astype(np.int64),
            0,
            count - 1,
        )
        squared_distance = np.sum(
            (directions[candidate_indices] - unit) ** 2,
            axis=1,
        )
        replace = squared_distance < best_squared_distance
        best_squared_distance[replace] = squared_distance[replace]
        best_indices[replace] = candidate_indices[replace]
    return best_indices


def equivalent_cone_half_angle_rad(direction_count: int) -> float:
    """Return the circular-cone half-angle with area 4*pi/direction_count."""

    count = _direction_count(direction_count)
    return math.acos(1.0 - 2.0 / float(count))


def hand_angular_projection_metadata(
    *,
    hand_center_m: np.ndarray,
    observed_at_us: int,
    direction_count: int,
    occupied_direction_count: int,
    angular_radius_scale: float,
    minimum_radius_m: float,
    radial_padding_m: float,
    maximum_range_m: float,
) -> dict[str, Any]:
    """Describe the shared geometry policy once instead of on every sphere."""

    count = _direction_count(direction_count)
    hand = np.asarray(hand_center_m, dtype=np.float64)
    if hand.shape != (3,) or not np.all(np.isfinite(hand)):
        raise ValueError("hand_center_m must contain three finite values")
    occupied = int(occupied_direction_count)
    if not 0 <= occupied <= count:
        raise ValueError("occupied_direction_count must be within the profile")
    timestamp = int(observed_at_us)
    if timestamp <= 0:
        raise ValueError("observed_at_us must be positive")
    nominal_half_angle = equivalent_cone_half_angle_rad(count)
    radius_scale = float(angular_radius_scale)
    minimum_radius = float(minimum_radius_m)
    radial_padding = float(radial_padding_m)
    maximum_range = float(maximum_range_m)
    if not 1.0 <= radius_scale <= 3.0:
        raise ValueError("angular_radius_scale must be in [1, 3]")
    if not math.isfinite(minimum_radius) or minimum_radius <= 0.0:
        raise ValueError("minimum_radius_m must be positive and finite")
    if not math.isfinite(radial_padding) or radial_padding < 0.0:
        raise ValueError("radial_padding_m must be finite and non-negative")
    if not math.isfinite(maximum_range) or maximum_range <= 0.0:
        raise ValueError("maximum_range_m must be positive and finite")
    return {
        "profile_id": ANGULAR_PROFILE_ID,
        "roi_scope": ANGULAR_ROI_SCOPE,
        "origin_frame_id": "rebot_arm_base",
        "origin_m": [round(float(value), 6) for value in hand],
        "observed_at_us": timestamp,
        "direction_count": count,
        "occupied_direction_count": occupied,
        "nominal_half_angle_rad": nominal_half_angle,
        "covering_half_angle_rad": nominal_half_angle * radius_scale,
        "angular_radius_scale": radius_scale,
        "minimum_radius_m": minimum_radius,
        "radial_padding_m": radial_padding,
        "maximum_range_m": maximum_range,
        "hit_selection": "NEAREST_SURFACE_HIT_PER_OCCUPIED_DIRECTION",
        "keep_out_boundary_mode": "HAND_RAY_TANGENT",
    }


def build_hand_angular_assertions(
    semantic_surfaces: list[dict[str, Any]],
    *,
    hand_center_m: np.ndarray,
    direction_count: int = 4096,
    angular_radius_scale: float = 1.5,
    minimum_radius_m: float = 0.005,
    radial_padding_m: float = 0.003,
    maximum_range_m: float = 1.2,
    include_pushable: bool = False,
    maximum_assertions: int = 20_000,
) -> list[dict[str, Any]]:
    """Project current semantic surfaces into one bounded hand-centric shell."""

    count = _direction_count(direction_count)
    hand = np.asarray(hand_center_m, dtype=np.float64)
    if hand.shape != (3,) or not np.all(np.isfinite(hand)):
        raise ValueError("hand_center_m must contain three finite values")
    radius_scale = float(angular_radius_scale)
    minimum_radius = float(minimum_radius_m)
    radial_padding = float(radial_padding_m)
    maximum_range = float(maximum_range_m)
    limit = int(maximum_assertions)
    if not 1.0 <= radius_scale <= 3.0:
        raise ValueError("angular_radius_scale must be in [1, 3]")
    if not math.isfinite(minimum_radius) or minimum_radius <= 0.0:
        raise ValueError("minimum_radius_m must be positive and finite")
    if not math.isfinite(radial_padding) or radial_padding < 0.0:
        raise ValueError("radial_padding_m must be finite and non-negative")
    if not math.isfinite(maximum_range) or maximum_range <= 0.0:
        raise ValueError("maximum_range_m must be positive and finite")
    if limit <= 0:
        raise ValueError("maximum_assertions must be positive")

    points_by_surface: list[np.ndarray] = []
    surface_indices: list[np.ndarray] = []
    normalized_surfaces: list[dict[str, Any]] = []
    for raw_surface in semantic_surfaces:
        if not isinstance(raw_surface, dict):
            raise ValueError("semantic_surfaces entries must be objects")
        object_type = str(raw_surface.get("type") or "").strip().upper()
        if object_type == "PUSHABLE" and not include_pushable:
            continue
        object_id = str(raw_surface.get("object_id") or "").strip()
        description = str(raw_surface.get("description") or "").strip()
        if not object_id or not description:
            raise ValueError("semantic surfaces require object_id and description")
        points = np.asarray(raw_surface.get("points_m"), dtype=np.float64)
        if points.size == 0:
            continue
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("semantic surface points_m must have shape Nx3")
        points = points[np.all(np.isfinite(points), axis=1)]
        if points.size == 0:
            continue
        relative = points - hand
        ranges = np.linalg.norm(relative, axis=1)
        keep = (ranges > 1e-9) & (ranges <= maximum_range)
        points = points[keep]
        if points.size == 0:
            continue
        normalized_index = len(normalized_surfaces)
        normalized_surfaces.append(
            {
                "object_id": object_id,
                "description": description,
                "type": object_type,
            }
        )
        points_by_surface.append(points)
        surface_indices.append(
            np.full(points.shape[0], normalized_index, dtype=np.int64)
        )
    if not points_by_surface:
        return []

    points = np.concatenate(points_by_surface, axis=0)
    owners = np.concatenate(surface_indices, axis=0)
    relative = points - hand
    ranges = np.linalg.norm(relative, axis=1)
    unit_directions = relative / ranges[:, None]
    bin_indices = nearest_spherical_fibonacci_indices(unit_directions, count)

    nearest_ranges = np.full(count, np.inf, dtype=np.float64)
    np.minimum.at(nearest_ranges, bin_indices, ranges)
    nearest_candidates = np.flatnonzero(
        ranges == nearest_ranges[bin_indices]
    )
    selected_by_bin = np.full(count, -1, dtype=np.int64)
    np.maximum.at(
        selected_by_bin,
        bin_indices[nearest_candidates],
        nearest_candidates,
    )
    selected = selected_by_bin[selected_by_bin >= 0]
    if selected.shape[0] > limit:
        selected = selected[
            np.argpartition(ranges[selected], limit - 1)[:limit]
        ]
        selected = selected[np.argsort(bin_indices[selected])]

    nominal_half_angle = equivalent_cone_half_angle_rad(count)
    covering_half_angle = nominal_half_angle * radius_scale
    assertions: list[dict[str, Any]] = []
    for point_index in selected:
        index = int(point_index)
        bin_index = int(bin_indices[index])
        owner = normalized_surfaces[int(owners[index])]
        surface_center = points[index]
        ray_direction = unit_directions[index]
        ray_range = float(ranges[index])
        radius = max(
            minimum_radius,
            ray_range * math.tan(covering_half_angle) + radial_padding,
        )
        object_type = str(owner["type"])
        sphere_center = surface_center
        if object_type == "KEEP_OUT":
            sphere_center = surface_center + ray_direction * radius
        assertions.append(
            {
                "assertion_id": f"sam2:{ANGULAR_ROI_SCOPE}:{bin_index}",
                "sphere_id": f"sam2:{ANGULAR_ROI_SCOPE}:{bin_index}",
                "object_id": str(owner["object_id"]),
                "description": str(owner["description"]),
                "center_m": [round(float(value), 6) for value in sphere_center],
                "radius_m": round(radius, 6),
                "type": object_type,
                "roi_scope": ANGULAR_ROI_SCOPE,
                "semantic_source": "SAM2_TRACKED_USER_DECLARED",
                "angular_bin_index": bin_index,
            }
        )
    return assertions


def build_visible_surface_aabb(
    *,
    object_id: str,
    object_type: str,
    description: str,
    points_m: np.ndarray,
    observed_at_us: int,
    freshness_ms: int = 5000,
    source_frame_number: int | None = None,
    source_policy_revision: str | None = None,
) -> dict[str, Any] | None:
    """Build an arm-base-aligned bound over only the currently visible surface."""

    points = np.asarray(points_m, dtype=np.float64)
    if points.size == 0:
        return None
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points_m must have shape Nx3")
    points = points[np.all(np.isfinite(points), axis=1)]
    if points.size == 0:
        return None
    timestamp = int(observed_at_us)
    lifetime_ms = int(freshness_ms)
    if timestamp <= 0 or lifetime_ms <= 0:
        raise ValueError("AABB timestamp and freshness must be positive")
    identifier = str(object_id or "").strip()
    if not identifier:
        raise ValueError("AABB object_id must be non-empty")
    if str(object_type or "").strip().upper() != "WORK_OBJECT":
        raise ValueError("visible surface AABBs are only valid for WORK_OBJECT")
    normalized_description = str(description or "").strip()
    if not normalized_description:
        raise ValueError("AABB description must be non-empty")

    minimum = points.min(axis=0)
    maximum = points.max(axis=0)
    center = (minimum + maximum) * 0.5
    size = maximum - minimum
    xmin, ymin, zmin = minimum.tolist()
    xmax, ymax, zmax = maximum.tolist()
    corners = {
        "right_forward_up": [xmax, ymin, zmax],
        "left_forward_up": [xmax, ymax, zmax],
        "right_backward_up": [xmin, ymin, zmax],
        "left_backward_up": [xmin, ymax, zmax],
        "right_forward_down": [xmax, ymin, zmin],
        "left_forward_down": [xmax, ymax, zmin],
        "right_backward_down": [xmin, ymin, zmin],
        "left_backward_down": [xmin, ymax, zmin],
    }
    return {
        "extent_kind": "VISIBLE_SURFACE_AABB",
        "object_id": identifier,
        "description": normalized_description,
        "type": "WORK_OBJECT",
        "frame_id": "rebot_arm_base",
        "convention_id": SPATIAL_CONVENTION_ID,
        "observed_at_us": timestamp,
        "freshness_ms": lifetime_ms,
        "expires_at_us": timestamp + lifetime_ms * 1000,
        "minimum_m": minimum.tolist(),
        "maximum_m": maximum.tolist(),
        "center_m": center.tolist(),
        "size_m": size.tolist(),
        "corners_m": corners,
        "axis_semantics": {
            "forward": "+X",
            "backward": "-X",
            "left": "+Y",
            "right": "-Y",
            "up": "+Z",
            "down": "-Z",
        },
        "visible_sample_count": int(points.shape[0]),
        "source_frame_number": (
            int(source_frame_number) if source_frame_number is not None else None
        ),
        "source_policy_revision": str(source_policy_revision or ""),
    }
