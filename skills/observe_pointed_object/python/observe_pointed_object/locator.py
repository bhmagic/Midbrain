from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import numpy as np

from spatial_registration_rgbd import (
    deproject_pixel,
    normalized_1000_box_to_pixels,
    normalized_1000_point_to_pixel,
    transform_point,
)


ITEM_LANDMARK_VLM_SCHEMA = "physical_agent.item_landmark_vlm"
ITEM_LANDMARK_VLM_SCHEMA_VERSION = 2

_MATERIAL_CLASSES = {
    "OPAQUE_DIFFUSE",
    "REFLECTIVE",
    "TRANSPARENT",
    "THIN_OR_PERFORATED",
    "UNKNOWN",
}
_CONTACT_POLICIES = {
    "WORKPIECE_CONTACT_ALLOWED",
    "NO_CONTACT",
}
_DEPTH_REQUIREMENTS = {
    "PREFER_METRIC",
    "REQUIRE_METRIC",
    "ALLOW_BEARING",
}
_TOP_LEVEL_FIELDS_V1 = {
    "schema",
    "schema_version",
    "scene_suitable",
    "reason",
    "item_label",
    "confidence",
    "material_class",
    "registered_depth_pixel_yx",
    "registered_depth_box_yxyx",
    "same_surface_search_allowed",
}
_TOP_LEVEL_FIELDS_V2 = {
    *_TOP_LEVEL_FIELDS_V1,
    "coordinate_space",
}
_PROVENANCE_FIELDS = {
    "backend_id",
    "model",
    "request_id",
    "source_coordinate_space",
    "source_registered_depth_pixel_yx",
    "source_registered_depth_box_yxyx",
    "coordinate_conversion",
}
_VLM_COORDINATE_SPACES = {
    "NORMALIZED_0_1000",
    "REGISTERED_DEPTH_PIXELS",
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
            raise RuntimeError("item-locator VLM did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("item-locator VLM result must be a JSON object")
    return value


def parse_item_landmark_vlm_result(
    text: str,
    *,
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    return validate_item_landmark_vlm_result(
        _json_object(text),
        registered_depth_grid=registered_depth_grid,
    )


def validate_item_landmark_vlm_result(
    value: Any,
    *,
    registered_depth_grid: tuple[int, int],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            "item-locator VLM fields do not match the required schema"
        )
    if value.get("schema") != ITEM_LANDMARK_VLM_SCHEMA:
        raise RuntimeError("item-locator VLM schema is invalid")
    schema_version = value.get("schema_version")
    expected_fields = (
        _TOP_LEVEL_FIELDS_V1
        if schema_version == 1
        else _TOP_LEVEL_FIELDS_V2
    )
    if set(value) - _PROVENANCE_FIELDS != expected_fields:
        raise RuntimeError(
            "item-locator VLM fields do not match the required schema"
        )
    if schema_version not in {1, ITEM_LANDMARK_VLM_SCHEMA_VERSION}:
        raise RuntimeError("item-locator VLM schema version is unsupported")
    coordinate_space = (
        "REGISTERED_DEPTH_PIXELS"
        if schema_version == 1
        else str(value.get("coordinate_space") or "").upper()
    )
    if coordinate_space not in _VLM_COORDINATE_SPACES:
        raise RuntimeError("item-locator VLM coordinate space is unsupported")
    if not isinstance(value["scene_suitable"], bool):
        raise RuntimeError("scene_suitable must be boolean")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("item-locator reason must be non-empty")
    label = value["item_label"]
    if not isinstance(label, str) or not label.strip():
        raise RuntimeError("item_label must be non-empty")
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise RuntimeError("item confidence must be between 0 and 1")
    material = str(value["material_class"]).upper()
    if material not in _MATERIAL_CLASSES:
        raise RuntimeError("item material class is unsupported")
    if not isinstance(value["same_surface_search_allowed"], bool):
        raise RuntimeError("same_surface_search_allowed must be boolean")

    height, width = (int(registered_depth_grid[0]), int(registered_depth_grid[1]))
    if height <= 0 or width <= 0:
        raise ValueError("registered_depth_grid must be positive")
    source_pixel = value["registered_depth_pixel_yx"]
    source_box = value["registered_depth_box_yxyx"]
    if not value["scene_suitable"]:
        if source_pixel is not None or source_box is not None:
            raise RuntimeError("an unsuitable scene must not return geometry")
        pixel = None
        box = None
    else:
        if (
            not isinstance(source_pixel, list)
            or len(source_pixel) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in source_pixel
            )
        ):
            raise RuntimeError("item pixel must contain two integers")
        if (
            not isinstance(source_box, list)
            or len(source_box) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in source_box
            )
        ):
            raise RuntimeError("item box must contain four integers")
        if coordinate_space == "NORMALIZED_0_1000":
            if any(not 0 <= int(item) <= 1000 for item in source_pixel):
                raise RuntimeError("normalized item pixel must be within 0..1000")
            if any(not 0 <= int(item) <= 1000 for item in source_box):
                raise RuntimeError("normalized item box must be within 0..1000")
            pixel = list(
                normalized_1000_point_to_pixel(
                    source_pixel,
                    (height, width),
                )
            )
            box = list(
                normalized_1000_box_to_pixels(
                    source_box,
                    (height, width),
                )
            )
        else:
            pixel = [int(source_pixel[0]), int(source_pixel[1])]
            box = [int(item) for item in source_box]
        y, x = (int(pixel[0]), int(pixel[1]))
        if not (0 <= y < height and 0 <= x < width):
            raise RuntimeError("item pixel is outside the registered-depth grid")
        y0, x0, y1, x1 = (int(item) for item in box)
        if not (0 <= y0 < y1 <= height and 0 <= x0 < x1 <= width):
            raise RuntimeError("item box is outside the registered-depth grid")
        if not (y0 <= y < y1 and x0 <= x < x1):
            raise RuntimeError("item pixel must be inside the item box")

    normalized = {
        "schema": ITEM_LANDMARK_VLM_SCHEMA,
        "schema_version": ITEM_LANDMARK_VLM_SCHEMA_VERSION,
        "coordinate_space": "REGISTERED_DEPTH_PIXELS",
        "scene_suitable": value["scene_suitable"],
        "reason": reason.strip(),
        "item_label": label.strip(),
        "confidence": float(confidence),
        "material_class": material,
        "registered_depth_pixel_yx": (
            None if pixel is None else [int(pixel[0]), int(pixel[1])]
        ),
        "registered_depth_box_yxyx": (
            None if box is None else [int(item) for item in box]
        ),
        "same_surface_search_allowed": value["same_surface_search_allowed"],
    }
    normalized["source_coordinate_space"] = str(
        value.get("source_coordinate_space") or coordinate_space
    )
    normalized["source_registered_depth_pixel_yx"] = value.get(
        "source_registered_depth_pixel_yx",
        source_pixel,
    )
    normalized["source_registered_depth_box_yxyx"] = value.get(
        "source_registered_depth_box_yxyx",
        source_box,
    )
    normalized["coordinate_conversion"] = value.get(
        "coordinate_conversion",
        {
            "source_space": coordinate_space,
            "target_space": "REGISTERED_DEPTH_PIXELS",
            "target_grid": [height, width],
            "pixel_policy": "ROUND_OVER_SIZE_MINUS_ONE",
            "box_policy": "FLOOR_START_CEIL_EXCLUSIVE_END",
        },
    )
    normalized.update(
        {
            key: value[key]
            for key in _PROVENANCE_FIELDS
            if value.get(key) is not None
        }
    )
    return normalized


def _inside_valid_region(
    y: np.ndarray,
    x: np.ndarray,
    *,
    height: int,
    width: int,
    valid_region: dict[str, Any] | None,
) -> np.ndarray:
    inside = (y >= 0) & (y < height) & (x >= 0) & (x < width)
    if not valid_region:
        return inside
    region_x = int(valid_region.get("x") or 0)
    region_y = int(valid_region.get("y") or 0)
    region_width = int(valid_region.get("width") or width)
    region_height = int(valid_region.get("height") or height)
    return inside & (
        (x >= region_x)
        & (x < region_x + region_width)
        & (y >= region_y)
        & (y < region_y + region_height)
    )


def _select_registered_depth(
    depth_m: np.ndarray,
    *,
    pixel_yx: tuple[int, int],
    box_yxyx: tuple[int, int, int, int],
    valid_region: dict[str, Any] | None,
    allow_neighbor: bool,
    minimum_depth_m: float,
    maximum_depth_m: float,
    support_radius_px: int,
    surface_tolerance_m: float,
    minimum_support_samples: int,
) -> dict[str, Any] | None:
    height, width = depth_m.shape
    y, x = pixel_yx
    y0, x0, y1, x1 = box_yxyx
    radius = max(0, int(support_radius_px))
    exact_depth = float(depth_m[y, x])
    exact_valid = bool(
        np.isfinite(exact_depth)
        and minimum_depth_m <= exact_depth <= maximum_depth_m
        and _inside_valid_region(
            np.asarray([y]),
            np.asarray([x]),
            height=height,
            width=width,
            valid_region=valid_region,
        )[0]
    )
    if exact_valid:
        patch_y0, patch_y1 = max(y0, y - radius), min(y1, y + radius + 1)
        patch_x0, patch_x1 = max(x0, x - radius), min(x1, x + radius + 1)
        patch = depth_m[patch_y0:patch_y1, patch_x0:patch_x1]
        yy, xx = np.mgrid[patch_y0:patch_y1, patch_x0:patch_x1]
        valid = (
            np.isfinite(patch)
            & (patch >= minimum_depth_m)
            & (patch <= maximum_depth_m)
            & (np.abs(patch - exact_depth) <= surface_tolerance_m)
            & _inside_valid_region(
                yy,
                xx,
                height=height,
                width=width,
                valid_region=valid_region,
            )
        )
        support = patch[valid]
        if support.size >= int(minimum_support_samples):
            median = float(np.median(support))
            return {
                "pixel_yx": [y, x],
                "depth_m": exact_depth,
                "source": "REGISTERED_DEPTH_EXACT",
                "support_samples": int(support.size),
                "support_median_m": median,
                "support_mad_m": float(np.median(np.abs(support - median))),
                "box_valid_samples": None,
            }

    if not allow_neighbor:
        return None
    box_depth = depth_m[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    valid = (
        np.isfinite(box_depth)
        & (box_depth >= minimum_depth_m)
        & (box_depth <= maximum_depth_m)
        & _inside_valid_region(
            yy,
            xx,
            height=height,
            width=width,
            valid_region=valid_region,
        )
    )
    samples = box_depth[valid]
    if samples.size < int(minimum_support_samples):
        return None
    foreground_reference = float(np.percentile(samples, 20))
    foreground = valid & (
        np.abs(box_depth - foreground_reference) <= surface_tolerance_m
    )
    foreground_depth = box_depth[foreground]
    if foreground_depth.size < int(minimum_support_samples):
        return None
    foreground_y = yy[foreground]
    foreground_x = xx[foreground]
    distances = (foreground_y - y) ** 2 + (foreground_x - x) ** 2
    index = int(np.argmin(distances))
    chosen_depth = float(foreground_depth[index])
    median = float(np.median(foreground_depth))
    return {
        "pixel_yx": [int(foreground_y[index]), int(foreground_x[index])],
        "depth_m": chosen_depth,
        "source": "REGISTERED_DEPTH_SAME_SURFACE_NEIGHBOR",
        "support_samples": int(foreground_depth.size),
        "support_median_m": median,
        "support_mad_m": float(
            np.median(np.abs(foreground_depth - median))
        ),
        "box_valid_samples": int(samples.size),
    }


def _target_ray(
    pixel_yx: tuple[int, int],
    intrinsics: dict[str, Any],
    target_from_camera: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y, x = pixel_yx
    fx = float(intrinsics["fx"])
    fy = float(intrinsics["fy"])
    cx = float(intrinsics["cx"])
    cy = float(intrinsics["cy"])
    if fx <= 0.0 or fy <= 0.0:
        raise ValueError("camera focal lengths must be positive")
    camera_ray = np.asarray(
        [(float(x) - cx) / fx, (float(y) - cy) / fy, 1.0],
        dtype=np.float64,
    )
    matrix = np.asarray(target_from_camera, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("target_from_camera must be a finite 4x4 transform")
    origin = matrix[:3, 3].copy()
    direction = matrix[:3, :3] @ camera_ray
    norm = float(np.linalg.norm(direction))
    if norm <= 1e-12:
        raise RuntimeError("item bearing ray has zero length")
    return origin, direction / norm, camera_ray


def _intersect_task_plane(
    origin: np.ndarray,
    direction: np.ndarray,
    task_plane: dict[str, Any],
) -> tuple[np.ndarray, float]:
    normal = np.asarray(task_plane.get("normal_target"), dtype=np.float64)
    if normal.shape != (3,) or not np.all(np.isfinite(normal)):
        raise ValueError("task plane normal_target must contain three finite values")
    norm = float(np.linalg.norm(normal))
    if norm <= 1e-12:
        raise ValueError("task plane normal must be non-zero")
    normal /= norm
    offset_m = float(task_plane.get("offset_m"))
    denominator = float(normal @ direction)
    if abs(denominator) <= 1e-9:
        raise RuntimeError("item bearing ray is parallel to the task plane")
    distance = float((offset_m - normal @ origin) / denominator)
    if not np.isfinite(distance) or distance <= 0.0:
        raise RuntimeError("task plane intersection lies behind the camera")
    return origin + distance * direction, distance


def _stable_object_id(item_label: str, requested_object_id: str | None) -> str:
    requested = str(requested_object_id or "").strip()
    if requested:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", requested):
            raise ValueError("object_id contains unsupported characters")
        return requested
    normalized = re.sub(r"[^a-z0-9]+", "-", item_label.lower()).strip("-")
    if normalized:
        return normalized[:96]
    digest = hashlib.sha256(item_label.encode("utf-8")).hexdigest()[:16]
    return f"item-{digest}"


def _volume_hint(
    *,
    box_yxyx: tuple[int, int, int, int],
    surface_depth_m: float,
    intrinsics: dict[str, Any],
    target_from_camera: np.ndarray,
) -> dict[str, Any]:
    """Estimate a representative volume behind a measured front surface.

    The measured target point remains the control/standoff reference.  This
    estimate is only for semantic-scene and visualization geometry.
    """

    y0, x0, y1, x1 = box_yxyx
    depth_m = float(surface_depth_m)
    width_m = (x1 - x0) * depth_m / float(intrinsics["fx"])
    height_m = (y1 - y0) * depth_m / float(intrinsics["fy"])
    # A single sphere is a representative cross section, not a bounding
    # sphere for the whole 2D box.  Half the shorter projected dimension is
    # materially less biased than half the box diagonal for tall cylinders.
    radius_m = 0.5 * min(width_m, height_m)
    centroid_depth_m = depth_m + radius_m
    center_pixel_yx = (
        0.5 * (float(y0) + float(y1 - 1)),
        0.5 * (float(x0) + float(x1 - 1)),
    )
    centroid_camera = deproject_pixel(
        center_pixel_yx,
        centroid_depth_m,
        intrinsics,
    )
    centroid_target = transform_point(target_from_camera, centroid_camera)
    return {
        "method": "FRONT_SURFACE_PROJECTED_CROSS_SECTION_CENTROID_V1",
        "width_m": width_m,
        "height_m": height_m,
        "surface_depth_m": depth_m,
        "estimated_centroid_depth_m": centroid_depth_m,
        "estimated_centroid_camera_m": centroid_camera.tolist(),
        "estimated_centroid_target_m": centroid_target.tolist(),
        "representative_sphere_radius_m": radius_m,
        # Compatibility alias for older scene/viewer consumers.
        "raw_sphere_radius_m": radius_m,
        "approximation": True,
        "control_point_remains_front_surface": True,
        "canonical_scene_must_apply_roi_minimum": True,
    }


def resolve_item_location(
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
    object_id: str | None = None,
    contact_policy: str = "WORKPIECE_CONTACT_ALLOWED",
    depth_requirement: str = "PREFER_METRIC",
    task_plane: dict[str, Any] | None = None,
    valid_region: dict[str, Any] | None = None,
    minimum_confidence: float = 0.65,
    minimum_depth_m: float = 0.05,
    maximum_depth_m: float = 20.0,
    support_radius_px: int = 2,
    surface_tolerance_m: float = 0.04,
    minimum_support_samples: int = 3,
) -> dict[str, Any]:
    depth = np.asarray(registered_depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    normalized = validate_item_landmark_vlm_result(
        vlm_result,
        registered_depth_grid=depth.shape,
    )
    policy = str(contact_policy).upper()
    if policy not in _CONTACT_POLICIES:
        raise ValueError("contact_policy is unsupported")
    requirement = str(depth_requirement).upper()
    if requirement not in _DEPTH_REQUIREMENTS:
        raise ValueError("depth_requirement is unsupported")
    stable_id = _stable_object_id(normalized["item_label"], object_id)
    quality_reasons: list[str] = []
    if not normalized["scene_suitable"]:
        quality_reasons.append("VLM_SCENE_UNSUITABLE")
    if normalized["confidence"] < float(minimum_confidence):
        quality_reasons.append("VLM_CONFIDENCE_BELOW_MINIMUM")
    if quality_reasons:
        return {
            "schema": "physical_agent.item_location",
            "schema_version": 1,
            "status": "REJECTED_OBSERVATION",
            "eligible_for_control_math": False,
            "motion_usable": False,
            "object_id": stable_id,
            "item_label": normalized["item_label"],
            "semantic_role": "WORKPIECE",
            "contact_policy": policy,
            "quality_reasons": quality_reasons,
            "vlm_reason": normalized["reason"],
        }

    pixel = tuple(int(item) for item in normalized["registered_depth_pixel_yx"])
    box = tuple(int(item) for item in normalized["registered_depth_box_yxyx"])
    origin, direction, camera_ray = _target_ray(
        pixel,
        intrinsics,
        target_from_camera,
    )
    material_hostile = normalized["material_class"] in {
        "REFLECTIVE",
        "TRANSPARENT",
        "THIN_OR_PERFORATED",
    }
    depth_selection = None
    if not material_hostile:
        depth_selection = _select_registered_depth(
            depth,
            pixel_yx=pixel,
            box_yxyx=box,
            valid_region=valid_region,
            allow_neighbor=normalized["same_surface_search_allowed"],
            minimum_depth_m=float(minimum_depth_m),
            maximum_depth_m=float(maximum_depth_m),
            support_radius_px=int(support_radius_px),
            surface_tolerance_m=float(surface_tolerance_m),
            minimum_support_samples=int(minimum_support_samples),
        )

    target_point: np.ndarray | None = None
    camera_point: np.ndarray | None = None
    metric_source = "NONE"
    uncertainty_m: float | None = None
    plane_evidence = None
    if depth_selection is not None:
        selected_pixel = tuple(int(item) for item in depth_selection["pixel_yx"])
        camera_point = deproject_pixel(
            selected_pixel,
            float(depth_selection["depth_m"]),
            intrinsics,
        )
        target_point = transform_point(target_from_camera, camera_point)
        metric_source = str(depth_selection["source"])
        angular_uncertainty_m = max(
            float(depth_selection["depth_m"]) / float(intrinsics["fx"]),
            float(depth_selection["depth_m"]) / float(intrinsics["fy"]),
        ) * max(1, int(support_radius_px))
        uncertainty_m = max(
            0.003,
            float(depth_selection["support_mad_m"]),
            angular_uncertainty_m,
        )
    elif task_plane is not None:
        target_point, ray_distance = _intersect_task_plane(
            origin,
            direction,
            task_plane,
        )
        metric_source = "TASK_PLANE_INTERSECTION"
        uncertainty_m = max(
            0.005,
            float(task_plane.get("uncertainty_m", 0.01)),
        )
        plane_evidence = {
            "plane_id": str(task_plane.get("plane_id") or "task-plane"),
            "normal_target": [
                float(item) for item in task_plane["normal_target"]
            ],
            "offset_m": float(task_plane["offset_m"]),
            "ray_distance_m": ray_distance,
            "uncertainty_m": uncertainty_m,
        }

    result = {
        "schema": "physical_agent.item_location",
        "schema_version": 1,
        "status": "METRIC_POINT_READY" if target_point is not None else "BEARING_ONLY",
        "eligible_for_control_math": target_point is not None,
        "motion_usable": False,
        "object_id": stable_id,
        "item_label": normalized["item_label"],
        "semantic_role": "WORKPIECE",
        "contact_policy": policy,
        "observed_at_us": int(observed_at_us),
        "source_frame": str(source_frame),
        "target_frame": str(target_frame),
        "calibration_revision": calibration_revision,
        "material_class": normalized["material_class"],
        "metric_source": metric_source,
        "location": (
            None
            if target_point is None
            else {
                "target_point_m": target_point.tolist(),
                "uncertainty_radius_m": float(uncertainty_m),
            }
        ),
        "camera_system_point_m": (
            None
            if camera_point is None
            else {
                "camera_system_x": float(camera_point[0]),
                "camera_system_y": float(camera_point[1]),
                "camera_system_z": float(camera_point[2]),
            }
        ),
        "bearing": {
            "target_origin_m": origin.tolist(),
            "target_unit_direction": direction.tolist(),
            "camera_ray_xyz": camera_ray.tolist(),
            "source_pixel_yx": list(pixel),
        },
        "depth_evidence": depth_selection,
        "task_plane_evidence": plane_evidence,
        "volume_hint": (
            None
            if target_point is None
            else _volume_hint(
                box_yxyx=box,
                surface_depth_m=(
                    float(depth_selection["depth_m"])
                    if depth_selection is not None
                    else float(
                        (
                            np.linalg.inv(
                                np.asarray(target_from_camera, dtype=np.float64)
                            )
                            @ np.append(target_point, 1.0)
                        )[2]
                    )
                ),
                intrinsics=intrinsics,
                target_from_camera=target_from_camera,
            )
        ),
        "degraded_reason": (
            None
            if target_point is not None and depth_selection is not None
            else "MATERIAL_LIMITED_DEPTH_USING_TASK_PLANE"
            if target_point is not None
            else "NO_TRUSTWORTHY_METRIC_DEPTH"
        ),
        "recommended_next_action": (
            None
            if target_point is not None
            else "ACQUIRE_SECOND_VIEW_OR_USE_BOUNDED_IMAGE_SERVO"
        ),
        "quality_reasons": [],
        "vlm_reason": normalized["reason"],
        "vlm_provenance": {
            key: vlm_result.get(key)
            for key in ("backend_id", "model", "request_id")
            if vlm_result.get(key) is not None
        },
        "registration_transform": {
            "target_from_camera": np.asarray(
                target_from_camera,
                dtype=np.float64,
            ).tolist(),
        },
        "data_route": dict(route_provenance),
    }
    if target_point is None and requirement == "REQUIRE_METRIC":
        result["status"] = "REJECTED_OBSERVATION"
        result["quality_reasons"] = ["METRIC_LOCATION_REQUIRED"]
    return result
