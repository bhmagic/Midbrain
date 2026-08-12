from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

from .geometry import (
    apply_transform,
    deproject_registered_depth_pixel,
    project_camera_point,
    rigid_transform,
)


LANDMARK_DETECTION_SCHEMA = "midbrain.effector_landmark_detection"
LANDMARK_DETECTION_SCHEMA_VERSION = 2
LANDMARK_COORDINATE_SPACE = "NORMALIZED_YX_0_1000_PER_IMAGE"
LANDMARK_COORDINATE_MAX = 1000

_TOP_LEVEL_FIELDS = {
    "schema",
    "schema_version",
    "scene_suitable",
    "landmark_id",
    "coordinate_space",
    "reason",
    "points",
}
_POINT_FIELDS = {
    "point_id",
    "rgb_yx_0_1000",
    "registered_depth_yx_0_1000",
    "confidence",
    "same_surface_confidence",
    "reason",
}


class InvalidDepthSelectionError(RuntimeError):
    def __init__(self, invalid_points: list[dict[str, Any]]) -> None:
        self.invalid_points = json.loads(json.dumps(invalid_points))
        descriptions = ", ".join(
            f"{point['point_id']} at {point['converted_pixel_yx']}"
            for point in self.invalid_points
        )
        super().__init__(
            "landmark VLM selected pixels without valid exact depth: "
            + descriptions
        )


def _require_exact_fields(
    value: Any,
    *,
    required: set[str],
    owner: str,
) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"{owner} must be an object")
    observed = set(value)
    if observed == required:
        return
    missing = ", ".join(sorted(required - observed)) or "none"
    unexpected = ", ".join(sorted(observed - required)) or "none"
    raise RuntimeError(
        f"{owner} fields do not match the required schema; "
        f"missing: {missing}; unexpected: {unexpected}"
    )


def _json_object(text: str) -> dict[str, Any]:
    candidate = str(text).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match is None:
            raise RuntimeError("landmark VLM did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("landmark VLM result must be an object")
    return value


def parse_landmark_detection(
    text: str,
    *,
    landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    return validate_landmark_detection(
        _json_object(text),
        landmark=landmark,
        rgb_grid=rgb_grid,
        registered_depth_grid=registered_depth_grid,
    )


def _canonical_coordinate(
    value: Any,
    *,
    name: str,
) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise RuntimeError(f"{name} must contain integer normalized y and x")
    y, x = int(value[0]), int(value[1])
    if not (
        0 <= y <= LANDMARK_COORDINATE_MAX
        and 0 <= x <= LANDMARK_COORDINATE_MAX
    ):
        raise RuntimeError(f"{name} must be within the inclusive 0 to 1000 grid")
    return [y, x]


def canonical_yx_to_pixel(
    value: Any,
    *,
    grid: tuple[int, int],
    name: str,
) -> list[int]:
    y, x = _canonical_coordinate(value, name=name)
    height, width = int(grid[0]), int(grid[1])
    if height <= 0 or width <= 0:
        raise RuntimeError(f"{name} target image grid is invalid")
    pixel_y = (y * (height - 1) + LANDMARK_COORDINATE_MAX // 2) // (
        LANDMARK_COORDINATE_MAX
    )
    pixel_x = (x * (width - 1) + LANDMARK_COORDINATE_MAX // 2) // (
        LANDMARK_COORDINATE_MAX
    )
    return [int(pixel_y), int(pixel_x)]


def _confidence(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{name} must be a number from zero to one")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise RuntimeError(f"{name} must be a number from zero to one")
    return result


def validate_landmark_detection(
    value: Any,
    *,
    landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    _require_exact_fields(
        value,
        required=_TOP_LEVEL_FIELDS,
        owner="landmark VLM",
    )
    if value.get("schema") != LANDMARK_DETECTION_SCHEMA:
        raise RuntimeError("landmark VLM schema is invalid")
    if value.get("schema_version") != LANDMARK_DETECTION_SCHEMA_VERSION:
        raise RuntimeError("landmark VLM schema version is unsupported")
    if value.get("coordinate_space") != LANDMARK_COORDINATE_SPACE:
        raise RuntimeError("landmark VLM coordinate space is invalid")
    if not isinstance(value.get("scene_suitable"), bool):
        raise RuntimeError("scene_suitable must be boolean")
    if value.get("landmark_id") != landmark.get("landmark_id"):
        raise RuntimeError("landmark VLM returned the wrong landmark ID")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise RuntimeError("landmark VLM reason must be non-empty")
    points = value.get("points")
    if not isinstance(points, list):
        raise RuntimeError("landmark VLM points must be an array")
    required_ids = list(landmark.get("required_point_ids") or [])
    if not value["scene_suitable"]:
        if points:
            raise RuntimeError("an unsuitable scene must not return points")
        return json.loads(json.dumps(value))
    if len(points) != len(required_ids):
        raise RuntimeError("landmark VLM did not return every required point")
    normalized_points: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    for point in points:
        _require_exact_fields(
            point,
            required=_POINT_FIELDS,
            owner="landmark point",
        )
        point_id = str(point.get("point_id") or "")
        if point_id not in required_ids or point_id in observed_ids:
            raise RuntimeError("landmark point IDs are missing, unknown, or repeated")
        reason = point.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("landmark point reason must be non-empty")
        normalized_points.append(
            {
                "point_id": point_id,
                "rgb_yx_0_1000": _canonical_coordinate(
                    point.get("rgb_yx_0_1000"),
                    name=f"{point_id}.rgb_yx_0_1000",
                ),
                "registered_depth_yx_0_1000": _canonical_coordinate(
                    point.get("registered_depth_yx_0_1000"),
                    name=f"{point_id}.registered_depth_yx_0_1000",
                ),
                "confidence": _confidence(point.get("confidence"), "confidence"),
                "same_surface_confidence": _confidence(
                    point.get("same_surface_confidence"),
                    "same_surface_confidence",
                ),
                "reason": reason.strip(),
            }
        )
        canonical_yx_to_pixel(
            normalized_points[-1]["rgb_yx_0_1000"],
            grid=rgb_grid,
            name=f"{point_id}.rgb_yx_0_1000",
        )
        canonical_yx_to_pixel(
            normalized_points[-1]["registered_depth_yx_0_1000"],
            grid=registered_depth_grid,
            name=f"{point_id}.registered_depth_yx_0_1000",
        )
        observed_ids.add(point_id)
    if observed_ids != set(required_ids):
        raise RuntimeError("landmark VLM point IDs do not match the profile")
    normalized_points.sort(key=lambda point: required_ids.index(point["point_id"]))
    return {
        **value,
        "reason": value["reason"].strip(),
        "points": normalized_points,
    }


def resolve_profile_landmark(
    *,
    detection: dict[str, Any],
    landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    world_from_camera: np.ndarray,
    minimum_confidence: float = 0.75,
    minimum_same_surface_confidence: float = 0.75,
    minimum_depth_m: float = 0.05,
    maximum_depth_m: float = 20.0,
    support_radius_px: int = 2,
    surface_support_tolerance_m: float = 0.02,
    minimum_support_samples: int = 1,
) -> dict[str, Any]:
    depth = np.asarray(registered_depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    normalized = validate_landmark_detection(
        detection,
        landmark=landmark,
        rgb_grid=rgb_grid,
        registered_depth_grid=depth.shape,
    )
    if not normalized.get("scene_suitable"):
        raise RuntimeError("the VLM rejected the landmark scene")
    transform = rigid_transform(world_from_camera, "world_from_camera")
    radius = max(0, int(support_radius_px))
    registered_points: list[dict[str, Any]] = []
    quality_reasons: list[str] = []
    invalid_depth_points: list[dict[str, Any]] = []
    for point in normalized["points"]:
        registered_depth_pixel_yx = canonical_yx_to_pixel(
            point["registered_depth_yx_0_1000"],
            grid=depth.shape,
            name=f"{point['point_id']}.registered_depth_yx_0_1000",
        )
        y, x = registered_depth_pixel_yx
        exact_depth = float(depth[y, x])
        if (
            not np.isfinite(exact_depth)
            or exact_depth < float(minimum_depth_m)
            or exact_depth > float(maximum_depth_m)
        ):
            invalid_depth_points.append(
                {
                    "point_id": point["point_id"],
                    "canonical_coordinate_yx_0_1000": point[
                        "registered_depth_yx_0_1000"
                    ],
                    "converted_pixel_yx": registered_depth_pixel_yx,
                    "observed_depth_m": (
                        exact_depth if np.isfinite(exact_depth) else None
                    ),
                }
            )
    if invalid_depth_points:
        raise InvalidDepthSelectionError(invalid_depth_points)
    for point in normalized["points"]:
        rgb_pixel_yx = canonical_yx_to_pixel(
            point["rgb_yx_0_1000"],
            grid=rgb_grid,
            name=f"{point['point_id']}.rgb_yx_0_1000",
        )
        registered_depth_pixel_yx = canonical_yx_to_pixel(
            point["registered_depth_yx_0_1000"],
            grid=depth.shape,
            name=f"{point['point_id']}.registered_depth_yx_0_1000",
        )
        y, x = registered_depth_pixel_yx
        exact_depth = float(depth[y, x])
        y0, y1 = max(0, y - radius), min(depth.shape[0], y + radius + 1)
        x0, x1 = max(0, x - radius), min(depth.shape[1], x + radius + 1)
        patch = depth[y0:y1, x0:x1]
        support_mask = (
            np.isfinite(patch)
            & (patch >= float(minimum_depth_m))
            & (patch <= float(maximum_depth_m))
            & (np.abs(patch - exact_depth) <= float(surface_support_tolerance_m))
        )
        support = patch[support_mask]
        if support.size < int(minimum_support_samples):
            quality_reasons.append(
                f"{point['point_id']} has insufficient same-surface depth support"
            )
        if float(point["confidence"]) < float(minimum_confidence):
            quality_reasons.append(f"{point['point_id']} VLM confidence is too low")
        if float(point["same_surface_confidence"]) < float(
            minimum_same_surface_confidence
        ):
            quality_reasons.append(
                f"{point['point_id']} RGB-depth surface confidence is too low"
            )
        camera_point = deproject_registered_depth_pixel(
            registered_depth_pixel_yx,
            exact_depth,
            intrinsics,
        )
        world_point = apply_transform(transform, camera_point)
        registered_points.append(
            {
                **point,
                "rgb_pixel_yx": rgb_pixel_yx,
                "registered_depth_pixel_yx": registered_depth_pixel_yx,
                "depth_m": exact_depth,
                "camera_system_point_m": camera_point.tolist(),
                "world_point_m": world_point.tolist(),
                "depth_evidence": {
                    "selection": (
                        "EXACT_PIXEL_FROM_CANONICAL_VLM_0_1000_COORDINATE"
                    ),
                    "canonical_coordinate_yx_0_1000": point[
                        "registered_depth_yx_0_1000"
                    ],
                    "converted_pixel_yx": registered_depth_pixel_yx,
                    "support_radius_px": radius,
                    "support_samples": int(support.size),
                    "patch_samples": int(patch.size),
                    "support_median_m": (
                        float(np.median(support)) if support.size else None
                    ),
                    "support_mad_m": (
                        float(np.median(np.abs(support - np.median(support))))
                        if support.size
                        else None
                    ),
                },
            }
        )
    camera_points = np.asarray(
        [point["camera_system_point_m"] for point in registered_points],
        dtype=np.float64,
    )
    world_points = np.asarray(
        [point["world_point_m"] for point in registered_points],
        dtype=np.float64,
    )
    pair_separation_m = None
    pair_policy = landmark.get("pair_separation_m")
    if len(registered_points) == 2:
        pair_separation_m = float(np.linalg.norm(camera_points[1] - camera_points[0]))
        if isinstance(pair_policy, dict):
            minimum = pair_policy.get("minimum")
            maximum = pair_policy.get("maximum")
            if minimum is not None and pair_separation_m < float(minimum):
                quality_reasons.append("landmark pair is closer than the profile limit")
            if maximum is not None and pair_separation_m > float(maximum):
                quality_reasons.append("landmark pair exceeds the profile limit")
    camera_reference = np.mean(camera_points, axis=0)
    world_reference = np.mean(world_points, axis=0)
    projected_y, projected_x = project_camera_point(camera_reference, intrinsics)
    midpoint_pixel = [
        int(np.clip(round(projected_y), 0, depth.shape[0] - 1)),
        int(np.clip(round(projected_x), 0, depth.shape[1] - 1)),
    ]
    return {
        "schema": "midbrain.resolved_effector_alignment_landmark",
        "schema_version": 1,
        "status": "LANDMARK_READY" if not quality_reasons else "REJECTED_OBSERVATION",
        "eligible_for_translation_refinement": not quality_reasons,
        "landmark_id": landmark["landmark_id"],
        "geometry": landmark["geometry"],
        "registered_points": registered_points,
        "camera_system_landmark_point_m": camera_reference.tolist(),
        "world_landmark_point_m": world_reference.tolist(),
        "registered_depth_landmark_pixel_yx": midpoint_pixel,
        "pair_separation_m": pair_separation_m,
        "quality_reasons": quality_reasons,
        "physical_motion_submitted": False,
    }


def build_landmark_prompt(
    *,
    profile: dict[str, Any],
    landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
) -> str:
    point_ids = ", ".join(str(item) for item in landmark["required_point_ids"])
    return (
        f"You are locating configured landmark {landmark['landmark_id']} for "
        "the active mounted-effector profile. Profile identity and display-name "
        "metadata are not visual classification requirements. Follow the "
        f"profile landmark description: {landmark['description_for_vlm']} "
        "First identify each named physical feature in RGB. Then, in the "
        "registered-depth view, independently select a valid depth pixel whose "
        "sample belongs to the same physical surface. The separate registered-"
        "depth validity image uses WHITE for usable exact samples and MAGENTA "
        "for invalid or missing samples on that same depth grid; every selected "
        "depth coordinate must land on WHITE. Do not choose the same "
        "numeric pixel by default, a nearest foreground object, a reflection, "
        "the background, or a support surface. Return no points and mark the "
        "scene unsuitable if correspondence is uncertain. Required point IDs: "
        f"{point_ids}. Every configured point is mandatory. The host rejects "
        "a missing, repeated, or extra point and calculates the registered 3D "
        "arithmetic mean only after every required point is present. RGB grid "
        f"is height={int(rgb_grid[0])}, "
        f"width={int(rgb_grid[1])}. Registered-depth grid is "
        f"height={int(registered_depth_grid[0])}, "
        f"width={int(registered_depth_grid[1])}. Return exactly one JSON "
        "object with exactly these seven top-level keys and no others: "
        "schema, schema_version, scene_suitable, landmark_id, "
        "coordinate_space, reason, points. Set schema exactly to "
        "midbrain.effector_landmark_detection, schema_version to the integer "
        f"2, landmark_id exactly to {landmark['landmark_id']}, and "
        "coordinate_space exactly to "
        "NORMALIZED_YX_0_1000_PER_IMAGE. scene_suitable must be a "
        "JSON boolean and reason must be a non-empty string. When the scene is "
        "suitable, points must contain exactly one object for each required "
        "point ID and no other point IDs. Every point object must have exactly "
        "these six keys and no others: point_id, rgb_yx_0_1000, "
        "registered_depth_yx_0_1000, confidence, same_surface_confidence, "
        "reason. Each coordinate must be a two-integer JSON array in [y, x] "
        "order on a canonical per-image grid from 0 through 1000 inclusive. "
        "Zero maps to the first row or column and 1000 maps to the last, "
        "regardless of source-image resolution, model-side resizing, or tiling. "
        "Do not return literal source-image pixels. The host deterministically "
        "converts each canonical coordinate to its separately stated RGB or "
        "registered-depth image grid. confidence and "
        "same_surface_confidence must be JSON numbers from 0 through 1, and "
        "each point reason must be a non-empty string. When the scene is "
        "unsuitable, still return all seven top-level keys, set "
        "scene_suitable to false, use an empty points array, and explain why "
        "in reason. Do not wrap the JSON in Markdown, an outer key, prose, or "
        "an explanation."
    )


def build_invalid_depth_retry_prompt(
    *,
    profile: dict[str, Any],
    landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
    invalid_points: list[dict[str, Any]],
) -> str:
    failed = ", ".join(
        f"{point['point_id']} at normalized YX "
        f"{point['canonical_coordinate_yx_0_1000']} / source YX "
        f"{point['converted_pixel_yx']}"
        for point in invalid_points
    )
    return (
        "Your previous landmark response was structurally valid, but these "
        f"registered-depth selections had no usable exact sample: {failed}. "
        "The marked-retry image shows those rejected selections. In the "
        "registered-depth validity image, WHITE means a usable exact depth "
        "sample and MAGENTA means invalid or missing depth. Return a complete "
        "replacement detection. Preserve each named physical feature's "
        "identity from RGB, but reselect its registered-depth coordinate at a "
        "WHITE pixel on the same physical feature and same surface, preferably "
        "slightly inside that surface rather than on a silhouette. Do not "
        "interpolate depth, infer a midpoint depth, snap to an unrelated nearby "
        "object, or reuse an invalid pixel. You may correct the RGB coordinate "
        "too if the original semantic feature was wrong. If no WHITE pixel on "
        "the same physical surface is defensible, return scene_suitable=false "
        "instead of guessing. "
        + build_landmark_prompt(
            profile=profile,
            landmark=landmark,
            rgb_grid=rgb_grid,
            registered_depth_grid=registered_depth_grid,
        )
    )
