from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

import numpy as np


SEMANTIC_SCENE_SCHEMA = "physical_agent.arm_semantic_sphere_scene"
SEMANTIC_SCENE_CONTRACT_VERSION = 2
SEMANTIC_SCENE_STREAM = "robot_arm.primary.integrated.scene"

GRIPPER_ROI = "GRIPPER_0P5M"
ARM_BASE_ROI = "ARM_BASE_1P2M"
ROI_POLICIES = {
    GRIPPER_ROI: {"radius_m": 0.5, "minimum_sphere_radius_m": 0.02},
    ARM_BASE_ROI: {"radius_m": 1.2, "minimum_sphere_radius_m": 0.06},
}

_TYPE_ALIASES = {
    "": "PUSHABLE",
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


def _points(value: Any) -> np.ndarray:
    points = np.asarray(value, dtype=np.float64)
    if points.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("raw_points_arm_base_m must have shape Nx3")
    return points[np.all(np.isfinite(points), axis=1)]


def _stable_token(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def build_self_exclusion_spheres(
    link_centers_arm_base_m: Any,
    segment_radii_m: list[float],
    *,
    maximum_spacing_m: float = 0.025,
) -> tuple[list[dict[str, Any]], str]:
    """Sample configurable arm-link capsules into conservative spheres."""

    centers = _points(link_centers_arm_base_m)
    if centers.shape[0] < 2:
        raise ValueError("at least two current arm-link centers are required")
    if len(segment_radii_m) != centers.shape[0] - 1:
        raise ValueError("segment_radii_m must match the link-center chain")
    spacing = float(maximum_spacing_m)
    if not math.isfinite(spacing) or spacing <= 0.0:
        raise ValueError("maximum_spacing_m must be positive and finite")

    spheres: list[dict[str, Any]] = []
    for segment_index, (start, end, raw_radius) in enumerate(
        zip(centers[:-1], centers[1:], segment_radii_m, strict=True)
    ):
        radius = float(raw_radius)
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError("segment radii must be positive and finite")
        distance = float(np.linalg.norm(end - start))
        sample_count = max(2, int(math.ceil(distance / spacing)) + 1)
        for sample_index, alpha in enumerate(np.linspace(0.0, 1.0, sample_count)):
            center = start * (1.0 - alpha) + end * alpha
            spheres.append(
                {
                    "sphere_id": f"self:{segment_index}:{sample_index}",
                    "center_m": center.tolist(),
                    "radius_m": radius,
                }
            )
    revision_basis = {
        "centers_m": np.round(centers, 6).tolist(),
        "segment_radii_m": [float(value) for value in segment_radii_m],
        "maximum_spacing_m": spacing,
    }
    return spheres, "self-filter-" + _stable_token(revision_basis)


def build_profile_self_exclusion_spheres(
    link_centers_arm_base_m: Any,
    segment_radii_m: list[float],
    effector_spheres_arm_base: list[dict[str, Any]],
    *,
    assembly_fingerprint: str,
    maximum_spacing_m: float = 0.025,
) -> tuple[list[dict[str, Any]], str]:
    """Build one profile-bound self filter for arm capsules and the effector."""

    arm_spheres, _ = build_self_exclusion_spheres(
        link_centers_arm_base_m,
        segment_radii_m,
        maximum_spacing_m=maximum_spacing_m,
    )
    fingerprint = str(assembly_fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("assembly_fingerprint must be non-empty")
    effector_spheres: list[dict[str, Any]] = []
    primitive_ids: set[str] = set()
    for index, primitive in enumerate(effector_spheres_arm_base):
        if not isinstance(primitive, dict):
            raise ValueError("effector self-exclusion spheres must be objects")
        primitive_id = str(primitive.get("primitive_id") or "").strip()
        center = _point(
            primitive.get("center_m"),
            f"effector self sphere {index} center_m",
        )
        radius = float(primitive.get("radius_m") or 0.0)
        if not primitive_id or primitive_id in primitive_ids:
            raise ValueError(
                "effector self-exclusion sphere IDs must be non-empty and unique"
            )
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError(
                "effector self-exclusion sphere radii must be positive and finite"
            )
        primitive_ids.add(primitive_id)
        effector_spheres.append(
            {
                "sphere_id": f"self:effector:{primitive_id}",
                "primitive_id": primitive_id,
                "center_m": center.tolist(),
                "radius_m": radius,
                "geometry_owner": "MOUNTED_EFFECTOR_PROFILE",
            }
        )
    spheres = [*arm_spheres, *effector_spheres]
    revision_basis = {
        "assembly_fingerprint": fingerprint,
        "spheres": [
            {
                "sphere_id": value["sphere_id"],
                "center_m": np.round(value["center_m"], 6).tolist(),
                "radius_m": float(value["radius_m"]),
            }
            for value in spheres
        ],
        "maximum_spacing_m": float(maximum_spacing_m),
    }
    return spheres, "self-filter-" + _stable_token(revision_basis)


def _apply_self_filter(
    points: np.ndarray,
    spheres: list[dict[str, Any]],
    *,
    margin_m: float,
    output_sphere_radii_m: np.ndarray | None = None,
) -> tuple[np.ndarray, int]:
    if points.size == 0:
        return points, 0
    if not spheres:
        raise ValueError(
            "SELF_FILTER_REQUIRED: current robot exclusion geometry is missing"
        )
    keep = np.ones(points.shape[0], dtype=bool)
    output_radii = (
        np.zeros(points.shape[0], dtype=np.float64)
        if output_sphere_radii_m is None
        else np.asarray(output_sphere_radii_m, dtype=np.float64)
    )
    if (
        output_radii.shape != (points.shape[0],)
        or not np.all(np.isfinite(output_radii))
        or np.any(output_radii < 0.0)
    ):
        raise ValueError(
            "output_sphere_radii_m must contain one finite non-negative "
            "radius per point"
        )
    for index, sphere in enumerate(spheres):
        center = _point(sphere.get("center_m"), f"self sphere {index} center_m")
        radius = float(sphere.get("radius_m"))
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError("self sphere radius must be positive and finite")
        # Raw points become conservative spheres later. Excluding only their
        # centers allows the generated sphere to extend back through the arm,
        # which makes every path collide at sample zero.
        keep &= (
            np.linalg.norm(points - center, axis=1)
            > radius + margin_m + output_radii
        )
    return points[keep], int(np.count_nonzero(~keep))


def _normalize_semantic_object(
    value: dict[str, Any],
    *,
    gripper_center: np.ndarray,
) -> dict[str, Any]:
    object_id = str(value.get("object_id") or value.get("assertion_id") or "").strip()
    if not object_id:
        raise ValueError("semantic objects require object_id")
    requested_type = str(value.get("type") or "").strip().upper()
    object_type = _TYPE_ALIASES.get(requested_type)
    if object_type is None:
        raise ValueError(f"unsupported semantic object type {requested_type!r}")
    description = str(value.get("description") or "").strip()
    if object_type == "KEEP_OUT" and not description:
        raise ValueError(
            f"KEEP_OUT semantic object {object_id!r} requires a user/upstream description"
        )
    center = _point(value.get("center_m"), f"semantic object {object_id} center_m")
    if float(np.linalg.norm(center - gripper_center)) <= 0.5:
        scope = GRIPPER_ROI
        roi_center = gripper_center
    elif float(np.linalg.norm(center)) <= 1.2:
        scope = ARM_BASE_ROI
        roi_center = np.zeros(3, dtype=np.float64)
    else:
        raise ValueError(f"semantic object {object_id!r} is outside both ROIs")
    radius = float(value.get("radius_m"))
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("semantic object radius_m must be positive and finite")
    radius = max(radius, float(ROI_POLICIES[scope]["minimum_sphere_radius_m"]))
    if float(np.linalg.norm(center - roi_center)) > float(
        ROI_POLICIES[scope]["radius_m"]
    ):
        raise ValueError(f"semantic object {object_id!r} is outside {scope}")
    return {
        "sphere_id": str(value.get("sphere_id") or f"object:{object_id}"),
        "object_id": object_id,
        "center_m": center.tolist(),
        "radius_m": radius,
        "type": object_type,
        "roi_scope": scope,
        "semantic_source": str(value.get("semantic_source") or "UPSTREAM_EXPLICIT"),
        "description": description,
    }


def _voxel_spheres(
    points: np.ndarray,
    *,
    roi_center: np.ndarray,
    roi_scope: str,
    minimum_radius_m: float,
    maximum_spheres: int,
    object_type: str,
) -> tuple[list[dict[str, Any]], float]:
    edge = minimum_radius_m * 2.0
    if points.size == 0:
        return [], edge
    if maximum_spheres <= 0:
        raise ValueError("maximum_spheres must leave room for geometry")
    while True:
        indices = np.floor((points - roi_center) / edge).astype(np.int64)
        unique, inverse = np.unique(indices, axis=0, return_inverse=True)
        if unique.shape[0] <= maximum_spheres:
            break
        edge *= 1.25
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
        radius = max(
            minimum_radius_m,
            float(sample_radii[group_index]),
        )
        key_text = ":".join(str(int(item)) for item in key)
        spheres.append(
            {
                "sphere_id": f"voxel:{roi_scope}:{key_text}",
                "object_id": f"obstacle:{roi_scope}:{key_text}",
                "center_m": center.tolist(),
                "radius_m": radius,
                "type": object_type,
                "roi_scope": roi_scope,
                "semantic_source": "UNCLAIMED_VISIBLE_DEPTH_DEFAULT_PUSHABLE",
            }
        )
    return spheres, edge


def _filter_sam2_semantic_self_geometry(
    objects: list[dict[str, Any]],
    self_spheres: list[dict[str, Any]],
    *,
    margin_m: float,
) -> tuple[list[dict[str, Any]], int]:
    """Apply a second arm-exclusion layer to SAM2 semantic cells."""

    retained: list[dict[str, Any]] = []
    removed = 0
    for value in objects:
        source = str(value.get("semantic_source") or "").upper()
        if not source.startswith("SAM2_TRACKED"):
            retained.append(value)
            continue
        center = _point(value.get("center_m"), "SAM2 semantic center")
        object_radius = float(value.get("radius_m") or 0.0)
        overlaps_self = False
        for index, sphere in enumerate(self_spheres):
            self_center = _point(
                sphere.get("center_m"),
                f"self sphere {index} center_m",
            )
            self_radius = float(sphere.get("radius_m") or 0.0)
            if float(np.linalg.norm(center - self_center)) <= (
                object_radius + self_radius + float(margin_m)
            ):
                overlaps_self = True
                break
        if overlaps_self:
            removed += 1
        else:
            retained.append(value)
    return retained, removed


def build_layered_scene(
    *,
    raw_points_arm_base_m: Any,
    gripper_center_arm_base_m: Any,
    self_exclusion_spheres: list[dict[str, Any]],
    self_filter_revision: str,
    semantic_objects: list[dict[str, Any]] | None = None,
    source_provenance: dict[str, Any] | None = None,
    maximum_spheres: int = 20_000,
    self_filter_margin_m: float = 0.01,
    publish_unclaimed_pushable_geometry: bool = False,
    robot_collision_geometry: dict[str, Any] | None = None,
    scene_revision: str | None = None,
) -> dict[str, Any]:
    """Compile simultaneous gripper/base ROI layers into contract version 2."""

    gripper_center = _point(gripper_center_arm_base_m, "gripper center")
    points = _points(raw_points_arm_base_m)
    points = points[np.linalg.norm(points, axis=1) <= 1.2]
    input_points = int(points.shape[0])
    filter_revision = str(self_filter_revision or "").strip()
    if points.size and not filter_revision:
        raise ValueError("SELF_FILTER_REQUIRED: self_filter_revision is missing")
    near_gripper_before_filter = (
        np.linalg.norm(points - gripper_center, axis=1) <= 0.5
    )
    output_sphere_radii = np.where(
        near_gripper_before_filter,
        float(ROI_POLICIES[GRIPPER_ROI]["minimum_sphere_radius_m"]),
        float(ROI_POLICIES[ARM_BASE_ROI]["minimum_sphere_radius_m"]),
    )
    points, self_removed = _apply_self_filter(
        points,
        self_exclusion_spheres,
        margin_m=float(self_filter_margin_m),
        output_sphere_radii_m=output_sphere_radii,
    )

    objects = [
        _normalize_semantic_object(value, gripper_center=gripper_center)
        for value in (semantic_objects or [])
    ]
    objects, semantic_self_removed = _filter_sam2_semantic_self_geometry(
        objects,
        self_exclusion_spheres,
        margin_m=float(self_filter_margin_m),
    )
    sphere_ids = [value["sphere_id"] for value in objects]
    if len(sphere_ids) != len(set(sphere_ids)):
        raise ValueError("semantic sphere_id values must be unique")
    if len(objects) > maximum_spheres:
        raise ValueError("semantic objects exceed maximum_spheres")

    semantic_removed = 0
    if points.size and objects:
        keep = np.ones(points.shape[0], dtype=bool)
        for value in objects:
            keep &= np.linalg.norm(
                points - _point(value["center_m"], "semantic center")
            ) > float(value["radius_m"])
        semantic_removed = int(np.count_nonzero(~keep))
        points = points[keep]

    near_mask = np.linalg.norm(points - gripper_center, axis=1) <= 0.5
    gripper_points = points[near_mask]
    base_points = points[~near_mask]
    gripper_spheres: list[dict[str, Any]] = []
    base_spheres: list[dict[str, Any]] = []
    gripper_edge = float(
        ROI_POLICIES[GRIPPER_ROI]["minimum_sphere_radius_m"]
    ) * 2.0
    base_edge = float(
        ROI_POLICIES[ARM_BASE_ROI]["minimum_sphere_radius_m"]
    ) * 2.0
    if publish_unclaimed_pushable_geometry:
        available = maximum_spheres - len(objects)
        gripper_budget = min(available, max(1, int(available * 0.75)))
        gripper_spheres, gripper_edge = _voxel_spheres(
            gripper_points,
            roi_center=gripper_center,
            roi_scope=GRIPPER_ROI,
            minimum_radius_m=float(
                ROI_POLICIES[GRIPPER_ROI]["minimum_sphere_radius_m"]
            ),
            maximum_spheres=gripper_budget,
            object_type="PUSHABLE",
        )
        available -= len(gripper_spheres)
        base_spheres, base_edge = _voxel_spheres(
            base_points,
            roi_center=np.zeros(3, dtype=np.float64),
            roi_scope=ARM_BASE_ROI,
            minimum_radius_m=float(
                ROI_POLICIES[ARM_BASE_ROI]["minimum_sphere_radius_m"]
            ),
            maximum_spheres=available,
            object_type="PUSHABLE",
        )
    spheres = [*objects, *gripper_spheres, *base_spheres]
    provenance = dict(source_provenance or {})
    revision = str(scene_revision or "").strip() or (
        "scene-" + _stable_token({"spheres": spheres, "source": provenance})
    )
    sam2_semantics = any(
        str(value.get("semantic_source") or "").upper().startswith("SAM2_TRACKED")
        for value in objects
    )
    depth_mode = (
        "POINT_CLOUD_PUSHABLE_TELEMETRY_AND_SEMANTICS"
        if input_points and publish_unclaimed_pushable_geometry
        else "SEMANTIC_MASKED_DEPTH"
        if sam2_semantics
        else "SEMANTIC_ONLY"
    )
    return {
        "contract_version": SEMANTIC_SCENE_CONTRACT_VERSION,
        "scene_revision": revision,
        "frame_id": "rebot_arm_base",
        "roi_layers": [
            {
                "scope": GRIPPER_ROI,
                "center_m": gripper_center.tolist(),
                "radius_m": 0.5,
                "minimum_sphere_radius_m": float(
                    ROI_POLICIES[GRIPPER_ROI]["minimum_sphere_radius_m"]
                ),
            },
            {
                "scope": ARM_BASE_ROI,
                "center_m": [0.0, 0.0, 0.0],
                "radius_m": 1.2,
                "minimum_sphere_radius_m": float(
                    ROI_POLICIES[ARM_BASE_ROI]["minimum_sphere_radius_m"]
                ),
            },
        ],
        "spheres": spheres,
        "robot_collision_geometry": dict(robot_collision_geometry or {}),
        "production": {
            "default_unclassified_type": "PUSHABLE",
            "keep_out_requires_user_or_upstream_description": True,
            "pushable_requires_explicit_upstream_type": False,
            "workpiece_requires_explicit_upstream_type": True,
            "self_filter_revision": filter_revision or None,
            "input_points_in_base_roi": input_points,
            "self_points_removed": self_removed,
            "semantic_points_removed": semantic_removed,
            "sam2_semantic_self_cells_removed": semantic_self_removed,
            "gripper_points_voxelized": int(gripper_points.shape[0]),
            "base_points_voxelized": int(base_points.shape[0]),
            "unclaimed_pushable_geometry_published": bool(
                publish_unclaimed_pushable_geometry
            ),
            "gripper_voxel_edge_m": gripper_edge,
            "base_voxel_edge_m": base_edge,
            "depth_mode": depth_mode,
            "source_provenance": provenance,
        },
    }


def build_scene_observation(
    scene: dict[str, Any],
    *,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
    sequence: int,
    observed_at_us: int,
    freshness_ms: int,
) -> dict[str, Any]:
    observed = int(observed_at_us)
    freshness = int(freshness_ms)
    if observed <= 0 or freshness <= 0 or int(sequence) < 0:
        raise ValueError("observation time, freshness, and sequence are invalid")
    return {
        "schema": SEMANTIC_SCENE_SCHEMA,
        "schema_version": 1,
        "stream": SEMANTIC_SCENE_STREAM,
        "provider_id": provider_id,
        "provider_instance_id": provider_instance_id,
        "boot_id": boot_id,
        "sequence": int(sequence),
        "observed_at_us": observed,
        "freshness_ms": freshness,
        "expires_at_us": observed + freshness * 1000,
        "frame_id": "rebot_arm_base",
        "coordinate_frame": "rebot_arm_base",
        "clock_domain": "system_monotonic_correlated",
        "valid": True,
        "data": scene,
    }
