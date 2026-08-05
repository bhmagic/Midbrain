from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

import numpy as np


SEMANTIC_SCENE_SCHEMA = "physical_agent.arm_semantic_sphere_scene"
SEMANTIC_SCENE_SCHEMA_VERSION = 1
SEMANTIC_SCENE_CONTRACT_VERSION = 2
SEMANTIC_SCENE_STREAM = "robot_arm.primary.integrated.scene"

GRIPPER_ROI = "GRIPPER_0P5M"
ARM_BASE_ROI = "ARM_BASE_1P2M"
ROI_POLICIES = {
    GRIPPER_ROI: {
        "radius_m": 0.5,
        "minimum_sphere_radius_m": 0.02,
    },
    ARM_BASE_ROI: {
        "radius_m": 1.2,
        "minimum_sphere_radius_m": 0.06,
    },
}

_SEMANTIC_TYPE_ALIASES = {
    "": "KEEP_OUT",
    "OBS": "KEEP_OUT",
    "OBSTACLE": "KEEP_OUT",
    "KEEP_OUT": "KEEP_OUT",
    "PUSHABLE": "PUSHABLE",
    "WORKPIECE": "WORK_OBJECT",
    "WORK_PIECE": "WORK_OBJECT",
    "WORK_OBJECT": "WORK_OBJECT",
}


def _point(value: Any, label: str) -> np.ndarray:
    point = np.asarray(value, dtype=np.float64)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ValueError(f"{label} must contain three finite values")
    return point


def _points(value: Any, label: str) -> np.ndarray:
    points = np.asarray(value, dtype=np.float64)
    if points.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f"{label} must have shape Nx3")
    if not np.all(np.isfinite(points)):
        raise ValueError(f"{label} must contain finite values")
    return points


def _stable_revision(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "scene-" + hashlib.sha256(encoded).hexdigest()[:24]


def _normalize_semantic_object(
    value: dict[str, Any],
    *,
    roi_scope: str,
    minimum_radius_m: float,
) -> dict[str, Any]:
    object_id = str(value.get("object_id") or "").strip()
    if not object_id:
        raise ValueError("semantic objects require object_id")
    requested_type = str(value.get("type") or "").strip().upper()
    object_type = _SEMANTIC_TYPE_ALIASES.get(requested_type)
    if object_type is None:
        raise ValueError(f"unsupported semantic object type {requested_type!r}")
    center = _point(value.get("center_m"), f"semantic object {object_id} center_m")
    radius_m = float(value.get("radius_m"))
    if not math.isfinite(radius_m) or radius_m <= 0.0:
        raise ValueError("semantic object radius_m must be positive and finite")
    radius_m = max(radius_m, float(minimum_radius_m))
    return {
        "sphere_id": str(value.get("sphere_id") or f"object:{object_id}"),
        "object_id": object_id,
        "center_m": center.tolist(),
        "radius_m": radius_m,
        "type": object_type,
        "roi_scope": roi_scope,
        "semantic_source": str(
            value.get("semantic_source") or "UPSTREAM_EXPLICIT"
        ),
    }


def semantic_object_from_item_location(
    item_location: dict[str, Any],
) -> dict[str, Any]:
    """Convert one metric item result into an explicit workpiece sphere."""

    if item_location.get("eligible_for_control_math") is not True:
        raise ValueError("item location must contain a metric control-math point")
    if item_location.get("target_frame") != "rebot_arm_base":
        raise ValueError("item location must be expressed in rebot_arm_base")
    location = item_location.get("location")
    if not isinstance(location, dict):
        raise ValueError("item location geometry is missing")
    volume = item_location.get("volume_hint")
    volume = volume if isinstance(volume, dict) else {}
    uncertainty_m = float(location.get("uncertainty_radius_m") or 0.0)
    projected_radius_m = float(
        volume.get("representative_sphere_radius_m")
        or volume.get("raw_sphere_radius_m")
        or 0.0
    )
    radius_m = max(uncertainty_m, projected_radius_m)
    if not math.isfinite(radius_m) or radius_m <= 0.0:
        raise ValueError("item location has no positive metric volume estimate")
    return {
        "object_id": str(item_location.get("object_id") or "").strip(),
        "center_m": _point(
            volume.get("estimated_centroid_target_m")
            or location.get("target_point_m"),
            "item target_point_m",
        ).tolist(),
        "radius_m": radius_m,
        "type": "WORK_OBJECT",
        "semantic_source": "METRIC_ITEM_LOCATOR",
    }


def _self_filter_points(
    points: np.ndarray,
    self_exclusion_spheres: list[dict[str, Any]],
    *,
    margin_m: float,
) -> tuple[np.ndarray, int]:
    if points.size == 0:
        return points, 0
    if not self_exclusion_spheres:
        raise ValueError(
            "SELF_FILTER_REQUIRED: raw point-cloud scenes require current "
            "robot self-exclusion geometry"
        )
    keep = np.ones(points.shape[0], dtype=bool)
    for index, sphere in enumerate(self_exclusion_spheres):
        center = _point(
            sphere.get("center_m"),
            f"self exclusion sphere {index} center_m",
        )
        radius_m = float(sphere.get("radius_m"))
        if not math.isfinite(radius_m) or radius_m <= 0.0:
            raise ValueError("self exclusion radius_m must be positive and finite")
        keep &= np.linalg.norm(points - center, axis=1) > (
            radius_m + margin_m
        )
    return points[keep], int(np.count_nonzero(~keep))


def _voxel_spheres(
    points: np.ndarray,
    *,
    roi_center: np.ndarray,
    roi_scope: str,
    minimum_radius_m: float,
    maximum_spheres: int,
) -> tuple[list[dict[str, Any]], float]:
    if points.size == 0:
        return [], minimum_radius_m * 2.0
    if maximum_spheres <= 0:
        raise ValueError("maximum_spheres must be positive")
    voxel_edge_m = minimum_radius_m * 2.0
    while True:
        indices = np.floor((points - roi_center) / voxel_edge_m).astype(np.int64)
        unique, inverse = np.unique(indices, axis=0, return_inverse=True)
        if unique.shape[0] <= maximum_spheres:
            break
        voxel_edge_m *= 1.25

    group_count = int(unique.shape[0])
    counts = np.bincount(inverse, minlength=group_count).astype(np.float64)
    centers = np.column_stack(
        [
            np.bincount(
                inverse,
                weights=points[:, axis],
                minlength=group_count,
            )
            / counts
            for axis in range(3)
        ]
    )
    distances = np.linalg.norm(points - centers[inverse], axis=1)
    sample_radii = np.zeros(group_count, dtype=np.float64)
    np.maximum.at(sample_radii, inverse, distances)
    spheres: list[dict[str, Any]] = []
    for group_index, (key, center) in enumerate(zip(unique, centers, strict=True)):
        radius_m = max(minimum_radius_m, float(sample_radii[group_index]))
        key_text = ":".join(str(int(item)) for item in key)
        spheres.append(
            {
                "sphere_id": f"voxel:{roi_scope}:{key_text}",
                "object_id": f"obstacle:{roi_scope}:{key_text}",
                "center_m": center.tolist(),
                "radius_m": radius_m,
                "type": "KEEP_OUT",
                "roi_scope": roi_scope,
                "semantic_source": "UNCLASSIFIED_POINT_CLOUD_DEFAULT",
            }
        )
    return spheres, voxel_edge_m


def build_canonical_semantic_scene(
    *,
    raw_points_arm_base_m: Any,
    roi_scope: str,
    gripper_center_arm_base_m: Any | None = None,
    self_exclusion_spheres: list[dict[str, Any]] | None = None,
    self_filter_revision: str | None = None,
    semantic_objects: list[dict[str, Any]] | None = None,
    source_provenance: dict[str, Any] | None = None,
    maximum_spheres: int = 20_000,
    self_filter_margin_m: float = 0.01,
) -> dict[str, Any]:
    """Build a conservative, ROI-bounded canonical semantic sphere scene."""

    scope = str(roi_scope or "").strip().upper()
    if scope not in ROI_POLICIES:
        raise ValueError(f"unsupported semantic ROI scope {scope!r}")
    policy = ROI_POLICIES[scope]
    roi_center = (
        np.zeros(3, dtype=np.float64)
        if scope == ARM_BASE_ROI
        else _point(gripper_center_arm_base_m, "gripper_center_arm_base_m")
    )
    points = _points(raw_points_arm_base_m, "raw_points_arm_base_m")
    roi_distance = np.linalg.norm(points - roi_center, axis=1)
    points = points[roi_distance <= float(policy["radius_m"])]
    input_points_in_roi = int(points.shape[0])

    filter_revision = str(self_filter_revision or "").strip()
    if points.size and not filter_revision:
        raise ValueError(
            "SELF_FILTER_REQUIRED: raw point-cloud scenes require a current "
            "self_filter_revision"
        )
    points, self_points_removed = _self_filter_points(
        points,
        list(self_exclusion_spheres or []),
        margin_m=float(self_filter_margin_m),
    )

    normalized_objects = [
        _normalize_semantic_object(
            value,
            roi_scope=scope,
            minimum_radius_m=float(policy["minimum_sphere_radius_m"]),
        )
        for value in (semantic_objects or [])
    ]
    for value in normalized_objects:
        center = _point(value["center_m"], "semantic object center_m")
        if float(np.linalg.norm(center - roi_center)) > float(policy["radius_m"]):
            raise ValueError(
                f"semantic object {value['object_id']!r} is outside the ROI"
            )

    semantic_points_removed = 0
    if points.size and normalized_objects:
        unclaimed = np.ones(points.shape[0], dtype=bool)
        for value in normalized_objects:
            center = _point(value["center_m"], "semantic object center_m")
            radius_m = float(value["radius_m"])
            unclaimed &= np.linalg.norm(points - center, axis=1) > radius_m
        semantic_points_removed = int(np.count_nonzero(~unclaimed))
        points = points[unclaimed]

    available_voxel_spheres = int(maximum_spheres) - len(normalized_objects)
    if available_voxel_spheres < 0:
        raise ValueError("semantic objects exceed maximum_spheres")
    voxel_spheres, voxel_edge_m = _voxel_spheres(
        points,
        roi_center=roi_center,
        roi_scope=scope,
        minimum_radius_m=float(policy["minimum_sphere_radius_m"]),
        maximum_spheres=available_voxel_spheres,
    )
    spheres = [*normalized_objects, *voxel_spheres]
    revision_basis = {
        "contract_version": SEMANTIC_SCENE_CONTRACT_VERSION,
        "frame_id": "rebot_arm_base",
        "roi_scope": scope,
        "roi_center_m": roi_center.tolist(),
        "spheres": spheres,
        "self_filter_revision": filter_revision or None,
        "source_provenance": dict(source_provenance or {}),
    }
    return {
        "contract_version": SEMANTIC_SCENE_CONTRACT_VERSION,
        "scene_revision": _stable_revision(revision_basis),
        "frame_id": "rebot_arm_base",
        "roi_layers": [
            {
                "scope": scope,
                "center_m": roi_center.tolist(),
                "radius_m": float(policy["radius_m"]),
                "minimum_sphere_radius_m": float(
                    policy["minimum_sphere_radius_m"]
                ),
            }
        ],
        "spheres": spheres,
        "production": {
            "default_unclassified_type": "KEEP_OUT",
            "pushable_requires_explicit_upstream_type": True,
            "workpiece_requires_explicit_upstream_type": True,
            "self_filter_revision": filter_revision or None,
            "input_points_in_roi": input_points_in_roi,
            "self_points_removed": self_points_removed,
            "semantic_points_removed": semantic_points_removed,
            "unclassified_points_voxelized": int(points.shape[0]),
            "voxel_edge_m": voxel_edge_m,
            "source_provenance": dict(source_provenance or {}),
        },
    }


def build_fabric_semantic_scene_observation(
    scene: dict[str, Any],
    *,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
    sequence: int,
    observed_at_us: int | None = None,
    freshness_ms: int = 1000,
) -> dict[str, Any]:
    if scene.get("contract_version") != SEMANTIC_SCENE_CONTRACT_VERSION:
        raise ValueError("scene must use the canonical contract version")
    observed = time.time_ns() // 1000 if observed_at_us is None else int(observed_at_us)
    if observed <= 0 or int(sequence) < 0:
        raise ValueError("observed_at_us must be positive and sequence non-negative")
    if int(freshness_ms) <= 0:
        raise ValueError("freshness_ms must be positive")
    return {
        "schema": SEMANTIC_SCENE_SCHEMA,
        "schema_version": SEMANTIC_SCENE_SCHEMA_VERSION,
        "stream": SEMANTIC_SCENE_STREAM,
        "provider_id": str(provider_id),
        "provider_instance_id": str(provider_instance_id),
        "boot_id": str(boot_id),
        "sequence": int(sequence),
        "observed_at_us": observed,
        "freshness_ms": int(freshness_ms),
        "expires_at_us": observed + int(freshness_ms) * 1000,
        "frame_id": "rebot_arm_base",
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": scene,
    }
