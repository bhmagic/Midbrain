from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


KEEP_OUT = "KEEP_OUT"
PUSHABLE = "PUSHABLE"
WORK_OBJECT = "WORK_OBJECT"
SUPPORTED_OBJECT_TYPES = {KEEP_OUT, PUSHABLE, WORK_OBJECT}


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

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SemanticSphere":
        sphere_id = str(payload.get("sphere_id") or payload.get("id") or "").strip()
        object_id = str(payload.get("object_id") or sphere_id).strip()
        object_type = str(payload.get("type") or "").strip().upper()
        if not sphere_id or not object_id:
            raise ValueError("scene spheres require stable sphere_id and object_id values")
        if object_type not in SUPPORTED_OBJECT_TYPES:
            raise ValueError(f"unsupported semantic object type {object_type!r}")
        radius = float(payload.get("radius_m"))
        if not np.isfinite(radius) or radius <= 0.0:
            raise ValueError("scene sphere radius_m must be positive and finite")
        return cls(sphere_id, object_id, _point(payload.get("center_m", []), "center_m"), radius, object_type)

    def snapshot(self) -> dict[str, Any]:
        return {
            "sphere_id": self.sphere_id,
            "object_id": self.object_id,
            "center_m": self.center_m.tolist(),
            "radius_m": self.radius_m,
            "type": self.object_type,
        }


@dataclass(frozen=True)
class SceneSnapshot:
    revision: str
    frame_id: str
    spheres: tuple[SemanticSphere, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, maximum_spheres: int = 20000) -> "SceneSnapshot":
        revision = str(payload.get("scene_revision") or payload.get("revision") or "").strip()
        frame_id = str(payload.get("frame_id") or "rebot_arm_base").strip()
        raw_spheres = payload.get("spheres")
        if not revision:
            raise ValueError("semantic scene requires a non-empty revision")
        if frame_id != "rebot_arm_base":
            raise ValueError("semantic scene must be transformed into rebot_arm_base before use")
        if not isinstance(raw_spheres, list):
            raise ValueError("semantic scene spheres must be an array")
        if len(raw_spheres) > int(maximum_spheres):
            raise ValueError("semantic scene exceeds the configured sphere limit")
        spheres = tuple(SemanticSphere.from_payload(item) for item in raw_spheres)
        identifiers = [sphere.sphere_id for sphere in spheres]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("semantic scene sphere_id values must be unique")
        return cls(revision, frame_id, spheres)

    def snapshot(self) -> dict[str, Any]:
        counts = {kind: 0 for kind in sorted(SUPPORTED_OBJECT_TYPES)}
        for sphere in self.spheres:
            counts[sphere.object_type] += 1
        return {
            "revision": self.revision,
            "frame_id": self.frame_id,
            "sphere_count": len(self.spheres),
            "counts": counts,
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
    allowed_contact_object_ids: set[str] | None = None,
    permit_pushable_contact: bool = False,
) -> dict[str, Any]:
    points = np.asarray(list(points_m), dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] < 2 or not np.all(np.isfinite(points)):
        raise ValueError("robot points must be a finite Nx3 polyline")
    radii = np.asarray(list(link_radii_m), dtype=float)
    if radii.shape != (points.shape[0] - 1,) or np.any(radii <= 0.0) or not np.all(np.isfinite(radii)):
        raise ValueError("link_radii_m must provide one positive radius per robot segment")
    allowed = set(allowed_contact_object_ids or set())
    minimum = float("inf")
    collisions: list[dict[str, Any]] = []
    for segment_index, (start, end, link_radius) in enumerate(zip(points[:-1], points[1:], radii)):
        for sphere in scene.spheres:
            clearance = point_segment_distance(sphere.center_m, start, end) - float(link_radius) - sphere.radius_m
            minimum = min(minimum, clearance)
            permitted = (
                (sphere.object_type == WORK_OBJECT and sphere.object_id in allowed)
                or (sphere.object_type == PUSHABLE and permit_pushable_contact)
            )
            if clearance <= 0.0 and not permitted:
                collisions.append(
                    {
                        "segment_index": segment_index,
                        "sphere_id": sphere.sphere_id,
                        "object_id": sphere.object_id,
                        "type": sphere.object_type,
                        "clearance_m": clearance,
                    }
                )
    return {
        "minimum_clearance_m": None if not np.isfinite(minimum) else minimum,
        "collision_free": not collisions,
        "collisions": collisions,
    }
