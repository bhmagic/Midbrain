"""Windows compatibility implementation of FoundationPose pose clustering."""

from __future__ import annotations

import math

import numpy as np


def _rotation_geodesic_distance(
    rotation_a: np.ndarray,
    rotation_b: np.ndarray,
) -> float:
    cosine = (np.trace(rotation_a @ rotation_b.T) - 1.0) * 0.5
    cosine = float(np.clip(cosine, -1.0, 1.0))
    return float(math.acos(cosine))


def cluster_poses(
    angle_diff: float,
    dist_diff: float,
    poses_in: np.ndarray,
    symmetry_tfs: np.ndarray,
) -> np.ndarray:
    poses = np.asarray(poses_in, dtype=np.float32)
    symmetries = np.asarray(symmetry_tfs, dtype=np.float32)

    if poses.ndim != 3 or poses.shape[1:] != (4, 4):
        raise ValueError(
            f"poses_in must have shape (N, 4, 4), got {poses.shape}"
        )

    if symmetries.ndim != 3 or symmetries.shape[1:] != (4, 4):
        raise ValueError(
            "symmetry_tfs must have shape (M, 4, 4), "
            f"got {symmetries.shape}"
        )

    if len(poses) == 0:
        return poses.copy()

    angle_threshold = math.radians(float(angle_diff))
    output = [poses[0]]

    for candidate in poses[1:]:
        is_new = True

        for existing in output:
            translation_distance = np.linalg.norm(
                candidate[:3, 3] - existing[:3, 3]
            )

            if translation_distance >= float(dist_diff):
                continue

            for symmetry in symmetries:
                symmetric_candidate = candidate @ symmetry
                rotation_distance = _rotation_geodesic_distance(
                    symmetric_candidate[:3, :3],
                    existing[:3, :3],
                )

                if rotation_distance < angle_threshold:
                    is_new = False
                    break

            if not is_new:
                break

        if is_new:
            output.append(candidate)

    return np.stack(output, axis=0).astype(
        np.float32,
        copy=False,
    )