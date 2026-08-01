from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

from spatial_registration_rgbd import deproject_pixel, transform_point


EFFECTOR_FRONT_VLM_SCHEMA = "physical_agent.effector_front_landmark_vlm"
EFFECTOR_FRONT_VLM_SCHEMA_VERSION = 1

_CONFIGURATIONS = {
    "BARE_GRIPPER",
    "MOUNTED_TOOL",
    "HELD_TOOL",
    "OTHER_EFFECTOR",
    "UNCERTAIN",
}
_GEOMETRIES = {"SINGLE_POINT", "PAIRED_POINTS", "UNKNOWN"}
_FALLBACK_REASONS = {
    "NONE",
    "SHARP_OR_THIN_FRONT_MISSING_DEPTH",
    "REFLECTIVE_FRONT_MISSING_DEPTH",
    "OTHER_FRONT_MISSING_DEPTH",
    "UNCERTAIN",
}
_SURFACES = {
    "GRIPPER_TIP",
    "TOOL_TIP",
    "TOOL_BODY_OR_HANDLE",
    "EFFECTOR_BODY",
    "OTHER_RIGID_FRONT",
}
_TOP_LEVEL_FIELDS = {
    "schema",
    "schema_version",
    "scene_suitable",
    "reason",
    "effector_configuration",
    "front_geometry",
    "depth_fallback_reason",
    "front_points",
}
_PROVENANCE_FIELDS = {"backend_id", "model", "request_id"}
_POINT_FIELDS = {
    "point_id",
    "registered_depth_pixel_yx",
    "confidence",
    "selected_surface",
    "selection_reason",
}


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
            raise RuntimeError(
                "effector-front VLM did not return a JSON object"
            )
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("effector-front VLM result must be a JSON object")
    return value


def parse_effector_front_vlm_result(
    text: str,
    *,
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    return validate_effector_front_vlm_result(
        _json_object(text),
        registered_depth_grid=registered_depth_grid,
    )


def validate_effector_front_vlm_result(
    value: Any,
    *,
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - _PROVENANCE_FIELDS != _TOP_LEVEL_FIELDS
    ):
        raise RuntimeError(
            "effector-front VLM fields do not match the required schema"
        )
    if value["schema"] != EFFECTOR_FRONT_VLM_SCHEMA:
        raise RuntimeError("effector-front VLM schema is invalid")
    if value["schema_version"] != EFFECTOR_FRONT_VLM_SCHEMA_VERSION:
        raise RuntimeError("effector-front VLM schema version is unsupported")
    if not isinstance(value["scene_suitable"], bool):
        raise RuntimeError("scene_suitable must be boolean")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise RuntimeError("effector-front VLM reason must be non-empty")

    configuration = str(value["effector_configuration"]).upper()
    geometry = str(value["front_geometry"]).upper()
    fallback_reason = str(value["depth_fallback_reason"]).upper()
    if configuration not in _CONFIGURATIONS:
        raise RuntimeError("effector configuration is unsupported")
    if geometry not in _GEOMETRIES:
        raise RuntimeError("effector front geometry is unsupported")
    if fallback_reason not in _FALLBACK_REASONS:
        raise RuntimeError("effector depth fallback reason is unsupported")

    height, width = (int(registered_depth_grid[0]), int(registered_depth_grid[1]))
    if height <= 0 or width <= 0:
        raise ValueError("registered_depth_grid must be positive")
    points = value["front_points"]
    if not isinstance(points, list):
        raise RuntimeError("front_points must be an array")

    normalized_points: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    for point in points:
        if not isinstance(point, dict) or set(point) != _POINT_FIELDS:
            raise RuntimeError(
                "effector-front point fields do not match the required schema"
            )
        point_id = str(point["point_id"])
        if point_id in observed_ids:
            raise RuntimeError("effector-front point IDs must be unique")
        pixel = point["registered_depth_pixel_yx"]
        if (
            not isinstance(pixel, list)
            or len(pixel) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in pixel
            )
        ):
            raise RuntimeError(
                "registered_depth_pixel_yx must contain two integers"
            )
        y, x = int(pixel[0]), int(pixel[1])
        if not (0 <= y < height and 0 <= x < width):
            raise RuntimeError(
                "effector-front pixel is outside the registered-depth grid"
            )
        confidence = point["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise RuntimeError(
                "effector-front point confidence must be between 0 and 1"
            )
        surface = str(point["selected_surface"]).upper()
        if surface not in _SURFACES:
            raise RuntimeError("effector-front selected surface is unsupported")
        selection_reason = point["selection_reason"]
        if (
            not isinstance(selection_reason, str)
            or not selection_reason.strip()
        ):
            raise RuntimeError(
                "effector-front selection reason must be non-empty"
            )
        normalized_points.append(
            {
                "point_id": point_id,
                "registered_depth_pixel_yx": [y, x],
                "confidence": float(confidence),
                "selected_surface": surface,
                "selection_reason": selection_reason.strip(),
            }
        )
        observed_ids.add(point_id)

    if value["scene_suitable"]:
        if configuration == "UNCERTAIN" or geometry == "UNKNOWN":
            raise RuntimeError(
                "a suitable effector-front scene cannot remain uncertain"
            )
        if geometry == "SINGLE_POINT":
            if len(normalized_points) != 1 or observed_ids != {"front"}:
                raise RuntimeError(
                    "SINGLE_POINT requires exactly the point ID front"
                )
        else:
            if len(normalized_points) != 2 or observed_ids != {
                "front_1",
                "front_2",
            }:
                raise RuntimeError(
                    "PAIRED_POINTS requires front_1 and front_2"
                )
            if configuration != "BARE_GRIPPER" or any(
                point["selected_surface"] != "GRIPPER_TIP"
                for point in normalized_points
            ):
                raise RuntimeError(
                    "paired fronts are reserved for a bare two-jaw gripper"
                )
    else:
        if normalized_points:
            raise RuntimeError(
                "an unsuitable scene must not return front points"
            )
        if (
            configuration != "UNCERTAIN"
            or geometry != "UNKNOWN"
            or fallback_reason != "UNCERTAIN"
        ):
            raise RuntimeError(
                "an unsuitable scene must use the explicit uncertain state"
            )

    normalized = {
        "schema": EFFECTOR_FRONT_VLM_SCHEMA,
        "schema_version": EFFECTOR_FRONT_VLM_SCHEMA_VERSION,
        "scene_suitable": value["scene_suitable"],
        "reason": value["reason"].strip(),
        "effector_configuration": configuration,
        "front_geometry": geometry,
        "depth_fallback_reason": fallback_reason,
        "front_points": normalized_points,
    }
    normalized.update(
        {
            key: value[key]
            for key in _PROVENANCE_FIELDS
            if value.get(key) is not None
        }
    )
    return normalized


def _inside_valid_region(
    y: int,
    x: int,
    *,
    height: int,
    width: int,
    valid_region: dict[str, Any] | None,
) -> bool:
    if not valid_region:
        return 0 <= y < height and 0 <= x < width
    region_x = int(valid_region.get("x") or 0)
    region_y = int(valid_region.get("y") or 0)
    region_width = int(valid_region.get("width") or width)
    region_height = int(valid_region.get("height") or height)
    return (
        region_x <= x < region_x + region_width
        and region_y <= y < region_y + region_height
    )


def resolve_effector_front_reference(
    *,
    vlm_result: dict[str, Any],
    registered_depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    target_from_camera: np.ndarray,
    observed_at_us: int,
    source_frame: str,
    target_frame: str,
    calibration_revision: str | None,
    route_provenance: dict[str, Any],
    valid_region: dict[str, Any] | None = None,
    minimum_confidence: float = 0.75,
    minimum_depth_m: float = 0.05,
    maximum_depth_m: float = 20.0,
    support_radius_px: int = 2,
    surface_support_tolerance_m: float = 0.03,
    minimum_pair_separation_m: float = 0.001,
    maximum_pair_separation_m: float = 0.5,
) -> dict[str, Any]:
    depth = np.asarray(registered_depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    normalized = validate_effector_front_vlm_result(
        vlm_result,
        registered_depth_grid=depth.shape,
    )
    if not normalized["scene_suitable"]:
        raise RuntimeError(
            "effector-front VLM rejected the scene: " + normalized["reason"]
        )
    transform = np.asarray(target_from_camera, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("target_from_camera must be a finite 4x4 transform")

    height, width = depth.shape
    radius = max(0, int(support_radius_px))
    registered_points: list[dict[str, Any]] = []
    quality_reasons: list[str] = []
    for point in normalized["front_points"]:
        y, x = point["registered_depth_pixel_yx"]
        if not _inside_valid_region(
            y,
            x,
            height=height,
            width=width,
            valid_region=valid_region,
        ):
            raise RuntimeError(
                f"effector-front point {point['point_id']} is outside the "
                "registered-depth valid region"
            )
        depth_m = float(depth[y, x])
        if (
            not np.isfinite(depth_m)
            or depth_m < float(minimum_depth_m)
            or depth_m > float(maximum_depth_m)
        ):
            raise RuntimeError(
                f"effector-front point {point['point_id']} has no valid exact depth"
            )

        y0, y1 = max(0, y - radius), min(height, y + radius + 1)
        x0, x1 = max(0, x - radius), min(width, x + radius + 1)
        patch = depth[y0:y1, x0:x1]
        valid = (
            np.isfinite(patch)
            & (patch >= float(minimum_depth_m))
            & (patch <= float(maximum_depth_m))
            & (
                np.abs(patch - depth_m)
                <= float(surface_support_tolerance_m)
            )
        )
        support = patch[valid]
        camera_system_xyz_m = deproject_pixel(
            (y, x),
            depth_m,
            intrinsics,
        )
        target_point = transform_point(transform, camera_system_xyz_m)
        if point["confidence"] < float(minimum_confidence):
            quality_reasons.append(
                f"{point['point_id']} VLM confidence is below the minimum"
            )
        registered_points.append(
            {
                **point,
                "depth_m": depth_m,
                "camera_system_point_m": {
                    "camera_system_x": float(camera_system_xyz_m[0]),
                    "camera_system_y": float(camera_system_xyz_m[1]),
                    "camera_system_z": float(camera_system_xyz_m[2]),
                },
                "target_point_m": target_point.tolist(),
                "depth_evidence": {
                    "selection": "EXACT_REGISTERED_DEPTH_PIXEL",
                    "support_radius_px": radius,
                    "surface_support_tolerance_m": float(
                        surface_support_tolerance_m
                    ),
                    "surface_support_samples": int(support.size),
                    "patch_samples": int(patch.size),
                    "support_median_m": float(np.median(support)),
                    "support_mad_m": float(
                        np.median(np.abs(support - np.median(support)))
                    ),
                },
            }
        )

    target_points = np.asarray(
        [point["target_point_m"] for point in registered_points],
        dtype=np.float64,
    )
    reference = np.mean(target_points, axis=0)
    pair_separation_m = None
    if target_points.shape[0] == 2:
        pair_separation_m = float(
            np.linalg.norm(target_points[1] - target_points[0])
        )
        if pair_separation_m < float(minimum_pair_separation_m):
            quality_reasons.append(
                "paired gripper fronts are too close to remain distinct"
            )
        if pair_separation_m > float(maximum_pair_separation_m):
            quality_reasons.append(
                "paired gripper fronts exceed the physical separation limit"
            )

    eligible = not quality_reasons
    return {
        "schema": "physical_agent.effector_front_reference",
        "schema_version": 1,
        "status": (
            "REFERENCE_READY" if eligible else "REJECTED_OBSERVATION"
        ),
        "eligible_for_control_math": eligible,
        "motion_usable": False,
        "publishes_control_frame": False,
        "specialized_action_point": False,
        "observed_at_us": int(observed_at_us),
        "source_frame": str(source_frame),
        "target_frame": str(target_frame),
        "calibration_revision": calibration_revision,
        "effector_configuration": normalized["effector_configuration"],
        "front_geometry": normalized["front_geometry"],
        "depth_fallback_reason": normalized["depth_fallback_reason"],
        "front_points": registered_points,
        "control_reference": {
            "method": (
                "MEAN_OF_PAIRED_3D_POINTS"
                if len(registered_points) == 2
                else "SINGLE_REGISTERED_3D_POINT"
            ),
            "target_point_m": reference.tolist(),
            "pair_separation_m": pair_separation_m,
        },
        "quality_reasons": quality_reasons,
        "vlm_reason": normalized["reason"],
        "vlm_provenance": {
            key: vlm_result.get(key)
            for key in ("backend_id", "model", "request_id")
            if vlm_result.get(key) is not None
        },
        "data_route": dict(route_provenance),
    }
