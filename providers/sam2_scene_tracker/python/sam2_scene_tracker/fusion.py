from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np


GRIPPER_SCOPE = "GRIPPER_0P5M"
ARM_BASE_SCOPE = "ARM_BASE_1P2M"


@dataclass
class _Voxel:
    center_m: np.ndarray
    sample_count: int
    first_seen_us: int
    last_seen_us: int


class PersistentSemanticVoxelMap:
    """Fuse repeated semantic depth samples without duplicating geometry."""

    def __init__(
        self,
        *,
        fusion_voxel_edge_m: float = 0.02,
        deduplication_radius_m: float | None = None,
        maximum_voxels_per_object: int = 100_000,
    ) -> None:
        edge = float(fusion_voxel_edge_m)
        if not math.isfinite(edge) or not 0.005 <= edge <= 0.1:
            raise ValueError("fusion_voxel_edge_m must be in [0.005, 0.1]")
        radius = edge * 0.8 if deduplication_radius_m is None else float(
            deduplication_radius_m
        )
        if not math.isfinite(radius) or not 0.0 < radius <= edge * 2.0:
            raise ValueError("deduplication radius is invalid")
        self.edge_m = edge
        self.deduplication_radius_m = radius
        self.maximum_voxels_per_object = int(maximum_voxels_per_object)
        if self.maximum_voxels_per_object <= 0:
            raise ValueError("maximum_voxels_per_object must be positive")
        self.identity: str | None = None
        self.objects: dict[str, dict[str, Any]] = {}
        self.reset_count = 0

    def invalidate(self) -> None:
        """Discard all fused geometry before a replacement policy is reviewed."""

        self.identity = None
        self.objects.clear()
        self.reset_count += 1

    def bind_identity(self, identity: str) -> bool:
        normalized = str(identity or "").strip()
        if not normalized:
            raise ValueError("semantic map identity must be non-empty")
        if normalized == self.identity:
            return False
        self.identity = normalized
        self.objects.clear()
        self.reset_count += 1
        return True

    def _key(self, point: np.ndarray) -> tuple[int, int, int]:
        return tuple(np.floor(point / self.edge_m).astype(np.int64).tolist())

    def _neighbor_keys(
        self,
        key: tuple[int, int, int],
    ) -> list[tuple[int, int, int]]:
        return [
            (key[0] + dx, key[1] + dy, key[2] + dz)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
        ]

    def update(
        self,
        *,
        object_id: str,
        object_type: str,
        description: str,
        points_m: np.ndarray,
        observed_at_us: int,
        surface_viewpoint_m: np.ndarray | None = None,
    ) -> dict[str, int]:
        if self.identity is None:
            raise RuntimeError("semantic map identity must be bound before update")
        identifier = str(object_id or "").strip()
        if not identifier:
            raise ValueError("object_id must be non-empty")
        points = np.asarray(points_m, dtype=np.float64)
        if points.size == 0:
            points = np.empty((0, 3), dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("points_m must have shape Nx3")
        points = points[np.all(np.isfinite(points), axis=1)]
        timestamp = int(observed_at_us)
        if timestamp <= 0:
            raise ValueError("observed_at_us must be positive")
        viewpoint = None
        if surface_viewpoint_m is not None:
            viewpoint = np.asarray(surface_viewpoint_m, dtype=np.float64)
            if viewpoint.shape != (3,) or not np.all(np.isfinite(viewpoint)):
                raise ValueError("surface_viewpoint_m must contain three finite values")
        entry = self.objects.setdefault(
            identifier,
            {
                "type": str(object_type),
                "description": str(description),
                "voxels": {},
                "surface_viewpoint_m": viewpoint,
            },
        )
        if entry["type"] != str(object_type):
            raise ValueError("one object_id cannot change semantic type in-place")
        if viewpoint is not None:
            entry["surface_viewpoint_m"] = viewpoint
        voxels: dict[tuple[int, int, int], _Voxel] = entry["voxels"]
        if points.shape[0] > 0:
            frame_indices = np.floor(points / self.edge_m).astype(np.int64)
            _, frame_inverse = np.unique(
                frame_indices,
                axis=0,
                return_inverse=True,
            )
            frame_count = int(frame_inverse.max()) + 1
            frame_counts = np.bincount(
                frame_inverse,
                minlength=frame_count,
            ).astype(np.int64)
            frame_points = np.column_stack(
                [
                    np.bincount(
                        frame_inverse,
                        weights=points[:, axis],
                        minlength=frame_count,
                    )
                    / frame_counts
                    for axis in range(3)
                ]
            )
        else:
            frame_counts = np.empty((0,), dtype=np.int64)
            frame_points = np.empty((0, 3), dtype=np.float64)
        inserted = 0
        merged = 0
        for point, raw_sample_count in zip(
            frame_points,
            frame_counts,
            strict=True,
        ):
            sample_count = int(raw_sample_count)
            key = self._key(point)
            target_key = key
            target_voxel = voxels.get(key)
            if target_voxel is None:
                best_distance = float("inf")
                for neighbor in self._neighbor_keys(key):
                    candidate = voxels.get(neighbor)
                    if candidate is None:
                        continue
                    distance = float(np.linalg.norm(point - candidate.center_m))
                    if (
                        distance <= self.deduplication_radius_m
                        and distance < best_distance
                    ):
                        target_key = neighbor
                        target_voxel = candidate
                        best_distance = distance
            if target_voxel is None:
                if len(voxels) >= self.maximum_voxels_per_object:
                    continue
                voxels[key] = _Voxel(
                    point.copy(),
                    sample_count,
                    timestamp,
                    timestamp,
                )
                inserted += 1
                merged += max(0, sample_count - 1)
                continue
            existing_weight = min(target_voxel.sample_count, 31)
            incoming_weight = min(sample_count, 32)
            target_voxel.center_m = (
                target_voxel.center_m * existing_weight
                + point * incoming_weight
            ) / float(existing_weight + incoming_weight)
            target_voxel.sample_count += sample_count
            target_voxel.last_seen_us = timestamp
            voxels[target_key] = target_voxel
            merged += sample_count
        return {
            "input_points": int(points.shape[0]),
            "frame_voxel_count": int(frame_points.shape[0]),
            "inserted_voxels": inserted,
            "merged_samples": merged,
            "persistent_voxel_count": len(voxels),
        }

    @staticmethod
    def _voxelize_for_scope(
        points: np.ndarray,
        *,
        center: np.ndarray,
        edge_m: float,
    ) -> list[tuple[tuple[int, int, int], np.ndarray, float]]:
        if points.size == 0:
            return []
        indices = np.floor((points - center) / edge_m).astype(np.int64)
        unique, inverse = np.unique(indices, axis=0, return_inverse=True)
        output: list[tuple[tuple[int, int, int], np.ndarray, float]] = []
        for index, key in enumerate(unique):
            members = points[inverse == index]
            centroid = members.mean(axis=0)
            radius = max(
                edge_m / 2.0,
                float(np.linalg.norm(members - centroid, axis=1).max(initial=0.0)),
            )
            output.append((tuple(int(value) for value in key), centroid, radius))
        return output

    @staticmethod
    def _dominant_visible_plane(
        points: np.ndarray,
        viewpoint: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, float] | None:
        """Estimate a dominant visible surface and orient its normal to the camera."""

        if points.shape[0] < 6:
            return None
        centroid = points.mean(axis=0)
        centered = points - centroid
        covariance = centered.T @ centered / float(points.shape[0])
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        if float(eigenvalues[1]) <= 1e-10:
            return None
        if float(eigenvalues[0] / eigenvalues[1]) > 0.12:
            return None
        normal = eigenvectors[:, 0]
        if float(np.dot(normal, viewpoint - centroid)) < 0.0:
            normal = -normal
        residuals = np.abs(centered @ normal)
        threshold = max(0.015, float(np.quantile(residuals, 0.75)) * 2.0)
        if float(np.count_nonzero(residuals <= threshold)) / points.shape[0] < 0.60:
            return None
        return centroid, normal, threshold

    @staticmethod
    def _boundary_sphere_center(
        surface_center: np.ndarray,
        radius_m: float,
        *,
        viewpoint: np.ndarray | None,
        dominant_plane: tuple[np.ndarray, np.ndarray, float] | None,
    ) -> tuple[np.ndarray, str]:
        """Put sphere volume behind a measured surface instead of around it."""

        if viewpoint is None:
            return surface_center, "SURFACE_CENTER_LEGACY"
        if dominant_plane is not None:
            plane_center, outward_normal, threshold = dominant_plane
            residual = abs(float(np.dot(surface_center - plane_center, outward_normal)))
            if residual <= threshold:
                return (
                    surface_center - outward_normal * float(radius_m),
                    "DOMINANT_PLANE_TANGENT",
                )
        interior_ray = surface_center - viewpoint
        norm = float(np.linalg.norm(interior_ray))
        if norm <= 1e-9:
            return surface_center, "SURFACE_CENTER_DEGENERATE_VIEWPOINT"
        return (
            surface_center + interior_ray / norm * float(radius_m),
            "VIEW_RAY_TANGENT",
        )

    def assertions(
        self,
        *,
        gripper_center_m: np.ndarray,
        include_pushable: bool = False,
        maximum_assertions: int = 20_000,
    ) -> list[dict[str, Any]]:
        gripper = np.asarray(gripper_center_m, dtype=np.float64)
        if gripper.shape != (3,) or not np.all(np.isfinite(gripper)):
            raise ValueError("gripper_center_m must contain three finite values")
        assertions: list[dict[str, Any]] = []
        for object_id, entry in self.objects.items():
            object_type = str(entry["type"])
            if object_type == "PUSHABLE" and not include_pushable:
                continue
            voxels: dict[tuple[int, int, int], _Voxel] = entry["voxels"]
            points = np.asarray(
                [voxel.center_m for voxel in voxels.values()],
                dtype=np.float64,
            )
            if points.size == 0:
                continue
            viewpoint_value = entry.get("surface_viewpoint_m")
            viewpoint = (
                np.asarray(viewpoint_value, dtype=np.float64)
                if viewpoint_value is not None
                else None
            )
            dominant_plane = (
                self._dominant_visible_plane(points, viewpoint)
                if viewpoint is not None and object_type == "KEEP_OUT"
                else None
            )
            near = np.linalg.norm(points - gripper, axis=1) <= 0.5
            scope_specs = (
                (GRIPPER_SCOPE, points[near], gripper, 0.04, 0.02),
                (
                    ARM_BASE_SCOPE,
                    points[~near & (np.linalg.norm(points, axis=1) <= 1.2)],
                    np.zeros(3, dtype=np.float64),
                    0.12,
                    0.06,
                ),
            )
            for scope, scoped_points, center, edge, minimum_radius in scope_specs:
                for key, centroid, radius in self._voxelize_for_scope(
                    scoped_points,
                    center=center,
                    edge_m=edge,
                ):
                    key_text = ":".join(str(value) for value in key)
                    sphere_center = centroid
                    boundary_mode = "NOT_KEEP_OUT"
                    if object_type == "KEEP_OUT":
                        sphere_center, boundary_mode = self._boundary_sphere_center(
                            centroid,
                            max(minimum_radius, radius),
                            viewpoint=viewpoint,
                            dominant_plane=dominant_plane,
                        )
                    assertions.append(
                        {
                            "assertion_id": (
                                f"sam2:{object_id}:{scope}:{key_text}"
                            ),
                            "sphere_id": f"sam2:{object_id}:{scope}:{key_text}",
                            "object_id": object_id,
                            "description": str(entry["description"]),
                            "center_m": sphere_center.tolist(),
                            "radius_m": max(minimum_radius, radius),
                            "type": object_type,
                            "roi_scope": scope,
                            "semantic_source": "SAM2_TRACKED_USER_DECLARED",
                            "persistence": "OCCLUSION_RETAINED_VOXEL_FUSION",
                            "surface_center_m": centroid.tolist(),
                            "surface_boundary_mode": boundary_mode,
                        }
                    )
                    if len(assertions) >= int(maximum_assertions):
                        return assertions
        return assertions

    def snapshot(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "fusion_voxel_edge_m": self.edge_m,
            "deduplication_radius_m": self.deduplication_radius_m,
            "reset_count": self.reset_count,
            "objects": {
                object_id: {
                    "type": entry["type"],
                    "description": entry["description"],
                    "persistent_voxel_count": len(entry["voxels"]),
                    "surface_viewpoint_m": (
                        entry["surface_viewpoint_m"].tolist()
                        if entry.get("surface_viewpoint_m") is not None
                        else None
                    ),
                }
                for object_id, entry in self.objects.items()
            },
        }
