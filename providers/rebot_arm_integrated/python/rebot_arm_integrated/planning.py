from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable
import uuid

import numpy as np

from .kinematics import ArmKinematics, rotation_vector
from .scene import SceneSnapshot, configuration_clearance
from .trajectory import QuinticJointSegment


@dataclass(frozen=True)
class PlanPreview:
    preview_id: str
    scene_revision: str | None
    duration_s: float
    q_start: np.ndarray
    q_goal: np.ndarray
    samples: tuple[np.ndarray, ...]
    minimum_clearance_m: float | None
    collision_free: bool
    collisions: tuple[dict[str, Any], ...]

    def snapshot(self, *, include_samples: bool = False) -> dict[str, Any]:
        result = {
            "preview_id": self.preview_id,
            "scene_revision": self.scene_revision,
            "duration_s": self.duration_s,
            "q_start": self.q_start.tolist(),
            "q_goal": self.q_goal.tolist(),
            "sample_count": len(self.samples),
            "minimum_clearance_m": self.minimum_clearance_m,
            "collision_free": self.collision_free,
            "collisions": list(self.collisions),
        }
        if include_samples:
            result["samples"] = [sample.tolist() for sample in self.samples]
        return result


@dataclass(frozen=True)
class CartesianContinuitySolution:
    q_waypoints: tuple[np.ndarray, ...]
    minimum_sigma: float
    maximum_waypoint_joint_step_rad: float
    maximum_endpoint_joint_delta_rad: float
    total_joint_travel_rad: float
    final_position_residual_m: float
    final_orientation_residual_rad: float
    final_iterations: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "waypoint_count": len(self.q_waypoints),
            "minimum_sigma": self.minimum_sigma,
            "maximum_waypoint_joint_step_rad": self.maximum_waypoint_joint_step_rad,
            "maximum_endpoint_joint_delta_rad": self.maximum_endpoint_joint_delta_rad,
            "total_joint_travel_rad": self.total_joint_travel_rad,
            "final_position_residual_m": self.final_position_residual_m,
            "final_orientation_residual_rad": self.final_orientation_residual_rad,
            "final_iterations": self.final_iterations,
        }


def _rotation_from_vector(vector: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(vector))
    if angle <= 1e-12:
        return np.eye(3, dtype=float)
    axis = vector / angle
    cross = np.array(
        [[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]],
        dtype=float,
    )
    return np.eye(3, dtype=float) + np.sin(angle) * cross + (1.0 - np.cos(angle)) * (cross @ cross)


def solve_cartesian_continuity(
    q_start: Iterable[float],
    start_frame: np.ndarray,
    goal_frame: np.ndarray,
    solve: Callable[[np.ndarray, np.ndarray], Any],
    *,
    waypoint_count: int,
) -> CartesianContinuitySolution:
    """Sequentially seed Cartesian waypoints to discourage IK branch jumps."""
    q = np.asarray(list(q_start), dtype=float)
    if q.shape != (6,) or not np.all(np.isfinite(q)):
        raise ValueError("q_start must contain six finite values")
    count = int(waypoint_count)
    if count < 2:
        raise ValueError("Cartesian continuity requires at least two waypoints")
    start = np.asarray(start_frame, dtype=float)
    goal = np.asarray(goal_frame, dtype=float)
    if start.shape != (4, 4) or goal.shape != (4, 4):
        raise ValueError("Cartesian frames must be 4x4 transforms")
    orientation_delta = rotation_vector(goal[:3, :3] @ start[:3, :3].T)
    waypoints = [q.copy()]
    sigma_values: list[float] = []
    step_values: list[float] = []
    total_travel = 0.0
    final_result = None
    for index in range(1, count + 1):
        alpha = index / count
        target = np.eye(4, dtype=float)
        target[:3, 3] = start[:3, 3] + alpha * (goal[:3, 3] - start[:3, 3])
        target[:3, :3] = _rotation_from_vector(alpha * orientation_delta) @ start[:3, :3]
        result = solve(q.copy(), target)
        final_result = result
        next_q = np.asarray(result.q_goal, dtype=float)
        step = float(np.max(np.abs(next_q - q)))
        step_values.append(step)
        total_travel += float(np.sum(np.abs(next_q - q)))
        if float(result.sigma_min) > 0.0:
            sigma_values.append(float(result.sigma_min))
        waypoints.append(next_q.copy())
        q = next_q
    return CartesianContinuitySolution(
        tuple(waypoints),
        min(sigma_values) if sigma_values else 0.0,
        max(step_values) if step_values else 0.0,
        float(np.max(np.abs(waypoints[-1] - waypoints[0]))),
        total_travel,
        float(final_result.position_residual_m),
        float(final_result.orientation_residual_rad),
        int(final_result.iterations),
    )


def build_waypoint_preview(
    kinematics: ArmKinematics,
    q_waypoints: Iterable[Iterable[float]],
    duration_s: float,
    *,
    scene: SceneSnapshot | None,
    link_radii_m: Iterable[float],
    sample_count: int = 81,
    allowed_contact_object_ids: set[str] | None = None,
    permit_pushable_contact: bool = False,
) -> PlanPreview:
    waypoints = tuple(np.asarray(list(item), dtype=float) for item in q_waypoints)
    if len(waypoints) < 2 or any(item.shape != (6,) or not np.all(np.isfinite(item)) for item in waypoints):
        raise ValueError("q_waypoints must contain at least two finite six-joint vectors")
    per_segment = max(2, int(np.ceil(int(sample_count) / (len(waypoints) - 1))))
    segment_duration = float(duration_s) / (len(waypoints) - 1)
    samples: list[np.ndarray] = []
    for index, (start, goal) in enumerate(zip(waypoints[:-1], waypoints[1:])):
        segment_samples = [item[0] for item in QuinticJointSegment.create(start, goal, segment_duration).sampled(per_segment)]
        samples.extend(segment_samples if index == 0 else segment_samples[1:])
    minimum_clearance: float | None = None
    collisions: list[dict[str, Any]] = []
    if scene is not None:
        for sample_index, q in enumerate(samples):
            report = configuration_clearance(
                kinematics.evaluate(q).points,
                scene,
                link_radii_m,
                allowed_contact_object_ids=allowed_contact_object_ids,
                permit_pushable_contact=permit_pushable_contact,
            )
            clearance = report["minimum_clearance_m"]
            if clearance is not None:
                minimum_clearance = clearance if minimum_clearance is None else min(minimum_clearance, clearance)
            collisions.extend({"sample_index": sample_index, **item} for item in report["collisions"])
    return PlanPreview(
        str(uuid.uuid4()),
        None if scene is None else scene.revision,
        float(duration_s),
        samples[0].copy(),
        samples[-1].copy(),
        tuple(samples),
        minimum_clearance,
        not collisions,
        tuple(collisions),
    )


def build_direct_preview(
    kinematics: ArmKinematics,
    q_start: Iterable[float],
    q_goal: Iterable[float],
    duration_s: float,
    *,
    scene: SceneSnapshot | None,
    link_radii_m: Iterable[float],
    sample_count: int = 81,
    allowed_contact_object_ids: set[str] | None = None,
    permit_pushable_contact: bool = False,
) -> PlanPreview:
    segment = QuinticJointSegment.create(q_start, q_goal, duration_s)
    samples = tuple(item[0] for item in segment.sampled(sample_count))
    minimum_clearance: float | None = None
    collisions: list[dict[str, Any]] = []
    if scene is not None:
        for sample_index, q in enumerate(samples):
            report = configuration_clearance(
                kinematics.evaluate(q).points,
                scene,
                link_radii_m,
                allowed_contact_object_ids=allowed_contact_object_ids,
                permit_pushable_contact=permit_pushable_contact,
            )
            clearance = report["minimum_clearance_m"]
            if clearance is not None:
                minimum_clearance = clearance if minimum_clearance is None else min(minimum_clearance, clearance)
            for collision in report["collisions"]:
                collisions.append({"sample_index": sample_index, **collision})
    return PlanPreview(
        str(uuid.uuid4()),
        None if scene is None else scene.revision,
        float(duration_s),
        samples[0].copy(),
        samples[-1].copy(),
        samples,
        minimum_clearance,
        not collisions,
        tuple(collisions),
    )
