from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


KEEP_OUT = "KEEP_OUT"
PUSHABLE = "PUSHABLE"
WORK_OBJECT = "WORK_OBJECT"
SUPPORTED_OBJECT_TYPES = {KEEP_OUT, PUSHABLE, WORK_OBJECT}

SCENE_CONTRACT_VERSION = 2
GRIPPER_ROI = "GRIPPER_0P5M"
ARM_BASE_ROI = "ARM_BASE_1P2M"
SUPPORTED_ROI_SCOPES = {GRIPPER_ROI, ARM_BASE_ROI}
ROI_LIMITS = {
    GRIPPER_ROI: {
        "radius_m": 0.5,
        "minimum_sphere_radius_m": 0.02,
    },
    ARM_BASE_ROI: {
        "radius_m": 1.2,
        "minimum_sphere_radius_m": 0.06,
    },
}

_OBJECT_TYPE_ALIASES = {
    "": KEEP_OUT,
    "OBS": KEEP_OUT,
    "OBSTACLE": KEEP_OUT,
    "KEEP_OUT": KEEP_OUT,
    "PUSHABLE": PUSHABLE,
    "WORKPIECE": WORK_OBJECT,
    "WORK_PIECE": WORK_OBJECT,
    "WORK_OBJECT": WORK_OBJECT,
}


def _point(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.shape != (3,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain three finite values")
    return result


@dataclass(frozen=True)
class SemanticSphere:
    sphere_id: str
    object_id: str
    center_m: np.ndarray
    radius_m: float
    object_type: str
    roi_scope: str = ARM_BASE_ROI

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        require_roi_scope: bool = False,
    ) -> "SemanticSphere":
        sphere_id = str(payload.get("sphere_id") or payload.get("id") or "").strip()
        object_id = str(payload.get("object_id") or sphere_id).strip()
        requested_type = str(payload.get("type") or "").strip().upper()
        object_type = _OBJECT_TYPE_ALIASES.get(requested_type)
        if not sphere_id or not object_id:
            raise ValueError("scene spheres require stable sphere_id and object_id values")
        if object_type is None:
            raise ValueError(f"unsupported semantic object type {requested_type!r}")
        radius = float(payload.get("radius_m"))
        if not np.isfinite(radius) or radius <= 0.0:
            raise ValueError("scene sphere radius_m must be positive and finite")
        requested_scope = str(payload.get("roi_scope") or "").strip().upper()
        if require_roi_scope and not requested_scope:
            raise ValueError(
                "canonical scene spheres require roi_scope"
            )
        roi_scope = requested_scope or ARM_BASE_ROI
        if roi_scope not in SUPPORTED_ROI_SCOPES:
            raise ValueError(f"unsupported semantic ROI scope {roi_scope!r}")
        return cls(
            sphere_id,
            object_id,
            _point(payload.get("center_m", []), "center_m"),
            radius,
            object_type,
            roi_scope,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "sphere_id": self.sphere_id,
            "object_id": self.object_id,
            "center_m": self.center_m.tolist(),
            "radius_m": self.radius_m,
            "type": self.object_type,
            "roi_scope": self.roi_scope,
        }


@dataclass(frozen=True)
class RoiLayer:
    scope: str
    center_m: np.ndarray
    radius_m: float
    minimum_sphere_radius_m: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RoiLayer":
        scope = str(payload.get("scope") or "").strip().upper()
        if scope not in SUPPORTED_ROI_SCOPES:
            raise ValueError(f"unsupported semantic ROI scope {scope!r}")
        expected = ROI_LIMITS[scope]
        radius_m = float(payload.get("radius_m"))
        minimum_sphere_radius_m = float(
            payload.get("minimum_sphere_radius_m")
        )
        if not np.isclose(radius_m, expected["radius_m"], atol=1e-9):
            raise ValueError(
                f"{scope} radius_m must be {expected['radius_m']}"
            )
        if not np.isclose(
            minimum_sphere_radius_m,
            expected["minimum_sphere_radius_m"],
            atol=1e-9,
        ):
            raise ValueError(
                f"{scope} minimum_sphere_radius_m must be "
                f"{expected['minimum_sphere_radius_m']}"
            )
        center_m = _point(payload.get("center_m", []), "roi center_m")
        if scope == ARM_BASE_ROI and not np.allclose(
            center_m,
            np.zeros(3),
            atol=1e-9,
        ):
            raise ValueError("ARM_BASE_1P2M ROI center must be the arm-base origin")
        return cls(
            scope=scope,
            center_m=center_m,
            radius_m=radius_m,
            minimum_sphere_radius_m=minimum_sphere_radius_m,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "center_m": self.center_m.tolist(),
            "radius_m": self.radius_m,
            "minimum_sphere_radius_m": self.minimum_sphere_radius_m,
        }


@dataclass(frozen=True)
class SceneSnapshot:
    revision: str
    frame_id: str
    spheres: tuple[SemanticSphere, ...]
    contract_version: int = 1
    roi_layers: tuple[RoiLayer, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, maximum_spheres: int = 20000) -> "SceneSnapshot":
        revision = str(payload.get("scene_revision") or payload.get("revision") or "").strip()
        frame_id = str(payload.get("frame_id") or "rebot_arm_base").strip()
        raw_spheres = payload.get("spheres")
        contract_version = int(payload.get("contract_version") or 1)
        if not revision:
            raise ValueError("semantic scene requires a non-empty revision")
        if frame_id != "rebot_arm_base":
            raise ValueError("semantic scene must be transformed into rebot_arm_base before use")
        if not isinstance(raw_spheres, list):
            raise ValueError("semantic scene spheres must be an array")
        if len(raw_spheres) > int(maximum_spheres):
            raise ValueError("semantic scene exceeds the configured sphere limit")
        if contract_version not in {1, SCENE_CONTRACT_VERSION}:
            raise ValueError(
                f"unsupported semantic scene contract version {contract_version}"
            )
        raw_roi_layers = payload.get("roi_layers")
        if contract_version == SCENE_CONTRACT_VERSION:
            if not isinstance(raw_roi_layers, list) or not raw_roi_layers:
                raise ValueError(
                    "canonical semantic scene requires at least one ROI layer"
                )
            roi_layers = tuple(
                RoiLayer.from_payload(item) for item in raw_roi_layers
            )
            scopes = [layer.scope for layer in roi_layers]
            if len(scopes) != len(set(scopes)):
                raise ValueError("semantic scene ROI scopes must be unique")
        else:
            roi_layers = ()
        spheres = tuple(
            SemanticSphere.from_payload(
                item,
                require_roi_scope=(
                    contract_version == SCENE_CONTRACT_VERSION
                ),
            )
            for item in raw_spheres
        )
        identifiers = [sphere.sphere_id for sphere in spheres]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("semantic scene sphere_id values must be unique")
        if contract_version == SCENE_CONTRACT_VERSION:
            layers_by_scope = {layer.scope: layer for layer in roi_layers}
            for sphere in spheres:
                layer = layers_by_scope.get(sphere.roi_scope)
                if layer is None:
                    raise ValueError(
                        f"sphere {sphere.sphere_id!r} references an absent ROI layer"
                    )
                if sphere.radius_m + 1e-12 < layer.minimum_sphere_radius_m:
                    raise ValueError(
                        f"sphere {sphere.sphere_id!r} is smaller than the "
                        f"{sphere.roi_scope} minimum radius"
                    )
                if float(np.linalg.norm(sphere.center_m - layer.center_m)) > (
                    layer.radius_m + 1e-12
                ):
                    raise ValueError(
                        f"sphere {sphere.sphere_id!r} is outside its "
                        f"{sphere.roi_scope} ROI"
                    )
        return cls(
            revision,
            frame_id,
            spheres,
            contract_version,
            roi_layers,
        )

    def snapshot(self) -> dict[str, Any]:
        counts = {kind: 0 for kind in sorted(SUPPORTED_OBJECT_TYPES)}
        for sphere in self.spheres:
            counts[sphere.object_type] += 1
        return {
            "revision": self.revision,
            "frame_id": self.frame_id,
            "contract_version": self.contract_version,
            "sphere_count": len(self.spheres),
            "counts": counts,
            "roi_layers": [layer.snapshot() for layer in self.roi_layers],
        }


def point_segment_distance(point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    segment = end - start
    denominator = float(segment @ segment)
    if denominator <= 1e-15:
        return float(np.linalg.norm(point - start))
    scale = float(np.clip(((point - start) @ segment) / denominator, 0.0, 1.0))
    return float(np.linalg.norm(point - (start + scale * segment)))


def configuration_clearance(
    points_m: Iterable[Iterable[float]],
    scene: SceneSnapshot,
    link_radii_m: Iterable[float],
    *,
    robot_spheres: Iterable[dict[str, Any]] | None = None,
    allowed_contact_object_ids: set[str] | None = None,
    permit_pushable_contact: bool = False,
    clearance_margin_by_type_m: dict[str, float] | None = None,
    maximum_collision_details: int = 16,
) -> dict[str, Any]:
    points = np.asarray(list(points_m), dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] < 2 or not np.all(np.isfinite(points)):
        raise ValueError("robot points must be a finite Nx3 polyline")
    radii = np.asarray(list(link_radii_m), dtype=float)
    if radii.shape != (points.shape[0] - 1,) or np.any(radii <= 0.0) or not np.all(np.isfinite(radii)):
        raise ValueError("link_radii_m must provide one positive radius per robot segment")
    checked_robot_spheres: list[tuple[str, np.ndarray, float]] = []
    robot_primitive_ids: set[str] = set()
    for primitive in robot_spheres or ():
        if not isinstance(primitive, dict):
            raise ValueError("robot_spheres entries must be objects")
        primitive_id = str(primitive.get("primitive_id", "")).strip()
        center = np.asarray(primitive.get("center_m", []), dtype=float)
        try:
            radius = float(primitive.get("radius_m"))
        except (TypeError, ValueError) as error:
            raise ValueError("robot sphere radii must be positive and finite") from error
        if not primitive_id or primitive_id in robot_primitive_ids:
            raise ValueError("robot sphere IDs must be non-empty and unique")
        if center.shape != (3,) or not np.all(np.isfinite(center)):
            raise ValueError("robot sphere centers must contain three finite values")
        if not np.isfinite(radius) or radius <= 0.0:
            raise ValueError("robot sphere radii must be positive and finite")
        robot_primitive_ids.add(primitive_id)
        checked_robot_spheres.append((primitive_id, center, radius))
    allowed = set(allowed_contact_object_ids or set())
    configured_margins = dict(clearance_margin_by_type_m or {})
    unknown_margin_types = set(configured_margins) - SUPPORTED_OBJECT_TYPES
    if unknown_margin_types:
        raise ValueError(
            "clearance margins contain unsupported semantic object types: "
            + ", ".join(sorted(unknown_margin_types))
        )
    margins_by_type = {
        object_type: float(configured_margins.get(object_type, 0.0))
        for object_type in SUPPORTED_OBJECT_TYPES
    }
    if any(
        not np.isfinite(value) or value < 0.0
        for value in margins_by_type.values()
    ):
        raise ValueError(
            "clearance margins must be finite non-negative distances"
        )
    if int(maximum_collision_details) < 0:
        raise ValueError("maximum_collision_details must be non-negative")
    minimum = float("inf")
    collisions: list[dict[str, Any]] = []
    collision_count = 0
    if scene.spheres:
        centers = np.stack(
            [sphere.center_m for sphere in scene.spheres], axis=0
        )
        sphere_radii = np.asarray(
            [sphere.radius_m for sphere in scene.spheres], dtype=float
        )
        required_margins = np.asarray(
            [margins_by_type[sphere.object_type] for sphere in scene.spheres],
            dtype=float,
        )
        permitted_mask = np.asarray(
            [
                (
                    sphere.object_type == WORK_OBJECT
                    and sphere.object_id in allowed
                )
                or (
                    sphere.object_type == PUSHABLE
                    and permit_pushable_contact
                )
                for sphere in scene.spheres
            ],
            dtype=bool,
        )
    else:
        centers = np.empty((0, 3), dtype=float)
        sphere_radii = np.empty((0,), dtype=float)
        required_margins = np.empty((0,), dtype=float)
        permitted_mask = np.empty((0,), dtype=bool)
    for segment_index, (start, end, link_radius) in enumerate(zip(points[:-1], points[1:], radii)):
        if centers.size == 0:
            continue
        segment = end - start
        denominator = float(segment @ segment)
        if denominator <= 1e-15:
            distances = np.linalg.norm(centers - start, axis=1)
        else:
            scales = np.clip(
                ((centers - start) @ segment) / denominator,
                0.0,
                1.0,
            )
            distances = np.linalg.norm(
                centers - (start + scales[:, None] * segment),
                axis=1,
            )
        raw_clearances = distances - float(link_radius) - sphere_radii
        clearances = raw_clearances - required_margins
        blocking_clearances = clearances[~permitted_mask]
        if blocking_clearances.size:
            minimum = min(minimum, float(np.min(blocking_clearances)))
        collision_indices = np.flatnonzero(
            (clearances <= 0.0) & ~permitted_mask
        )
        collision_count += int(collision_indices.size)
        remaining = int(maximum_collision_details) - len(collisions)
        if remaining <= 0:
            continue
        for sphere_index in collision_indices[:remaining]:
            sphere = scene.spheres[int(sphere_index)]
            collisions.append(
                {
                    "segment_index": segment_index,
                    "sphere_id": sphere.sphere_id,
                    "object_id": sphere.object_id,
                    "type": sphere.object_type,
                    "raw_clearance_m": float(raw_clearances[sphere_index]),
                    "required_clearance_margin_m": float(
                        required_margins[sphere_index]
                    ),
                    "clearance_m": float(clearances[sphere_index]),
                }
            )
    for primitive_id, center, robot_radius in checked_robot_spheres:
        if centers.size == 0:
            continue
        distances = np.linalg.norm(centers - center, axis=1)
        raw_clearances = distances - robot_radius - sphere_radii
        clearances = raw_clearances - required_margins
        blocking_clearances = clearances[~permitted_mask]
        if blocking_clearances.size:
            minimum = min(minimum, float(np.min(blocking_clearances)))
        collision_indices = np.flatnonzero(
            (clearances <= 0.0) & ~permitted_mask
        )
        collision_count += int(collision_indices.size)
        remaining = int(maximum_collision_details) - len(collisions)
        if remaining <= 0:
            continue
        for sphere_index in collision_indices[:remaining]:
            sphere = scene.spheres[int(sphere_index)]
            collisions.append(
                {
                    "robot_primitive_id": primitive_id,
                    "robot_sphere_radius_m": robot_radius,
                    "sphere_id": sphere.sphere_id,
                    "object_id": sphere.object_id,
                    "type": sphere.object_type,
                    "raw_clearance_m": float(raw_clearances[sphere_index]),
                    "required_clearance_margin_m": float(
                        required_margins[sphere_index]
                    ),
                    "clearance_m": float(clearances[sphere_index]),
                }
            )
    return {
        "minimum_clearance_m": None if not np.isfinite(minimum) else minimum,
        "collision_free": collision_count == 0,
        "collision_count": collision_count,
        "collision_details_truncated": collision_count > len(collisions),
        "collisions": collisions,
    }
