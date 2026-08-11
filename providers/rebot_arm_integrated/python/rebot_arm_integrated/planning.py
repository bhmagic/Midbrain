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
    collision_count: int
    first_collision_sample_index: int | None
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
            "collision_count": self.collision_count,
            "first_collision_sample_index": (
                self.first_collision_sample_index
            ),
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


def build_transit_frame_candidates(
    start_frame: np.ndarray,
    goal_frame: np.ndarray,
) -> tuple[tuple[str, tuple[np.ndarray, ...]], ...]:
    """Build the direct free-space path evaluated by the controller.

    General obstacle rerouting is intentionally not implemented. If this path
    meets classified geometry, the controller may execute only its closest
    collision-free prefix and must report that the requested goal was not
    reached.
    """

    start = np.asarray(start_frame, dtype=float)
    goal = np.asarray(goal_frame, dtype=float)
    if start.shape != (4, 4) or goal.shape != (4, 4):
        raise ValueError("transit start and goal frames must be 4x4 transforms")
    if not np.all(np.isfinite(start)) or not np.all(np.isfinite(goal)):
        raise ValueError("transit frames must be finite")

    return (("DIRECT", (goal.copy(),)),)


def controller_owned_duration(
    q_waypoints: Iterable[Iterable[float]],
    cartesian_path_length_m: float,
    requested_speed_m_s: float,
    joint_rate_caps_rad_s: Iterable[float],
    *,
    minimum_duration_s: float,
) -> dict[str, Any]:
    """Choose path duration from requested speed and physical joint caps."""

    waypoints = tuple(np.asarray(list(item), dtype=float) for item in q_waypoints)
    if len(waypoints) < 2:
        raise ValueError("duration planning requires at least two joint waypoints")
    caps = np.asarray(list(joint_rate_caps_rad_s), dtype=float)
    if any(item.shape != caps.shape for item in waypoints):
        raise ValueError("joint waypoints and rate caps must have matching shapes")
    if not np.all(np.isfinite(caps)) or np.any(caps <= 0.0):
        raise ValueError("joint rate caps must be positive and finite")
    effective_speed = float(requested_speed_m_s)
    if not np.isfinite(effective_speed) or effective_speed <= 0.0:
        raise ValueError("requested Cartesian speed must be positive and finite")
    cartesian_duration = max(0.0, float(cartesian_path_length_m)) / effective_speed
    joint_duration = sum(
        float(np.max(1.5 * np.abs(goal - start) / caps))
        for start, goal in zip(waypoints[:-1], waypoints[1:])
    )
    duration = max(float(minimum_duration_s), cartesian_duration, joint_duration)
    limiting_factor = (
        "PROVIDER_JOINT_RATE_CAPS"
        if joint_duration >= cartesian_duration and joint_duration >= minimum_duration_s
        else (
            "REQUESTED_CARTESIAN_SPEED"
            if cartesian_duration >= minimum_duration_s
            else "MINIMUM_DURATION"
        )
    )
    return {
        "requested_speed_m_s": float(requested_speed_m_s),
        "effective_speed_m_s": effective_speed,
        "speed_clamped": False,
        "cartesian_path_length_m": float(cartesian_path_length_m),
        "cartesian_duration_s": cartesian_duration,
        "joint_rate_duration_s": joint_duration,
        "duration_s": duration,
        "limiting_factor": limiting_factor,
    }


def joint_speed_policy_schedule(
    q_waypoints: Iterable[Iterable[float]],
    cartesian_positions_m: Iterable[Iterable[float]],
    requested_speed_m_s: float,
    joint_rate_caps_rad_s: Iterable[float],
    *,
    minimum_stage_duration_s: float,
    authentication_threshold_rad_s: float,
    hard_limit_rad_s: float,
) -> dict[str, Any]:
    """Build requested and hardware-bounded per-joint stage speeds."""

    waypoints = tuple(np.asarray(list(item), dtype=float) for item in q_waypoints)
    positions = tuple(
        np.asarray(list(item), dtype=float) for item in cartesian_positions_m
    )
    caps = np.asarray(list(joint_rate_caps_rad_s), dtype=float)
    speed = float(requested_speed_m_s)
    minimum_duration = float(minimum_stage_duration_s)
    authentication_threshold = float(authentication_threshold_rad_s)
    hard_limit = float(hard_limit_rad_s)
    if len(waypoints) < 2 or len(positions) != len(waypoints):
        raise ValueError("joint-speed scheduling requires matching waypoints and positions")
    if any(item.shape != caps.shape for item in waypoints):
        raise ValueError("joint waypoints and rate caps must have matching shapes")
    if any(item.shape != (3,) for item in positions):
        raise ValueError("Cartesian positions must contain three values")
    if (
        not np.all(np.isfinite(caps))
        or np.any(caps <= 0.0)
        or not np.isfinite(speed)
        or speed <= 0.0
        or not np.isfinite(minimum_duration)
        or minimum_duration <= 0.0
        or not 0.0 < authentication_threshold < hard_limit
    ):
        raise ValueError("joint-speed scheduling inputs are invalid")

    requested_durations: list[float] = []
    effective_durations: list[float] = []
    requested_stage_speeds: list[np.ndarray] = []
    effective_stage_speeds: list[np.ndarray] = []
    for q_start, q_goal, p_start, p_goal in zip(
        waypoints[:-1],
        waypoints[1:],
        positions[:-1],
        positions[1:],
    ):
        delta = np.abs(q_goal - q_start)
        requested_duration = max(
            minimum_duration,
            float(np.linalg.norm(p_goal - p_start)) / speed,
        )
        requested_joint_speeds = 1.5 * delta / requested_duration
        effective_duration = max(
            requested_duration,
            float(np.max(1.5 * delta / np.maximum(caps, 1e-9))),
        )
        requested_durations.append(requested_duration)
        effective_durations.append(effective_duration)
        requested_stage_speeds.append(requested_joint_speeds)
        effective_stage_speeds.append(1.5 * delta / effective_duration)

    requested_by_joint = np.max(np.vstack(requested_stage_speeds), axis=0)
    effective_by_joint = np.max(np.vstack(effective_stage_speeds), axis=0)
    requested_peak = float(np.max(requested_by_joint))
    effective_peak = float(np.max(effective_by_joint))
    return {
        "requested_stage_durations_s": requested_durations,
        "effective_stage_durations_s": effective_durations,
        "requested_peak_by_joint_rad_s": requested_by_joint.tolist(),
        "effective_peak_by_joint_rad_s": effective_by_joint.tolist(),
        "requested_peak_joint_speed_rad_s": requested_peak,
        "effective_peak_joint_speed_rad_s": effective_peak,
        "joint_rate_caps_rad_s": caps.tolist(),
        "authentication_threshold_rad_s": authentication_threshold,
        "hard_limit_rad_s": hard_limit,
        "authentication_required": requested_peak > authentication_threshold,
        "hard_limit_exceeded": requested_peak >= hard_limit,
        "provider_or_motor_limited": any(
            effective + 1e-9 < requested
            for effective, requested in zip(
                effective_by_joint.tolist(), requested_by_joint.tolist()
            )
        ),
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


def solve_cartesian_continuity_adaptive(
    q_start: Iterable[float],
    start_frame: np.ndarray,
    goal_frame: np.ndarray,
    solve: Callable[[np.ndarray, np.ndarray], Any],
    *,
    initial_waypoint_count: int,
    maximum_waypoint_count: int,
    maximum_joint_step_rad: float,
) -> CartesianContinuitySolution:
    """Increase Cartesian sampling until every adjacent IK step is bounded."""

    initial = int(initial_waypoint_count)
    maximum = int(maximum_waypoint_count)
    maximum_step = float(maximum_joint_step_rad)
    if initial < 2:
        raise ValueError("initial_waypoint_count must be at least two")
    if maximum < initial:
        raise ValueError(
            "maximum_waypoint_count must be at least initial_waypoint_count"
        )
    if not np.isfinite(maximum_step) or maximum_step <= 0.0:
        raise ValueError("maximum_joint_step_rad must be positive and finite")

    count = initial
    while True:
        result = solve_cartesian_continuity(
            q_start,
            start_frame,
            goal_frame,
            solve,
            waypoint_count=count,
        )
        if result.maximum_waypoint_joint_step_rad <= maximum_step:
            return result
        if count >= maximum:
            return result
        count = min(maximum, count * 2)


def _configuration_collision_geometry(
    kinematics: ArmKinematics,
    q: np.ndarray,
    link_radii_m: Iterable[float],
    tool_to_control: np.ndarray | None,
    effector_spheres: tuple[dict[str, Any], ...],
) -> tuple[list[np.ndarray], tuple[float, ...], list[dict[str, Any]]]:
    frame_result = kinematics.evaluate(q)
    all_points = list(frame_result.points)
    radii = tuple(float(value) for value in link_radii_m)
    if not radii or len(radii) > len(all_points) - 1:
        raise ValueError("collision radii do not match the evaluated kinematic chain")
    collision_points = [point.copy() for point in all_points[: len(radii) + 1]]
    controlled_frame: np.ndarray | None = None
    if len(radii) == len(all_points) - 1 and tool_to_control is not None:
        controlled_frame = kinematics.controlled_frame(q, tool_to_control)
        collision_points[-1] = controlled_frame[:3, 3].copy()
    runtime_spheres: list[dict[str, Any]] = []
    if effector_spheres:
        if tool_to_control is None:
            raise ValueError(
                "effector collision spheres require a controlled-frame transform"
            )
        if controlled_frame is None:
            controlled_frame = kinematics.controlled_frame(q, tool_to_control)
        for primitive in effector_spheres:
            primitive_id = str(primitive.get("primitive_id", "")).strip()
            offset = np.asarray(primitive.get("translation_m", []), dtype=float)
            try:
                radius = float(primitive.get("radius_m"))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "effector collision sphere radius must be positive and finite"
                ) from error
            if not primitive_id:
                raise ValueError("effector collision sphere ID must be non-empty")
            if offset.shape != (3,) or not np.all(np.isfinite(offset)):
                raise ValueError(
                    "effector collision sphere translation must contain three finite values"
                )
            if not np.isfinite(radius) or radius <= 0.0:
                raise ValueError(
                    "effector collision sphere radius must be positive and finite"
                )
            center = (
                controlled_frame[:3, :3] @ offset
                + controlled_frame[:3, 3]
            )
            runtime_spheres.append(
                {
                    "primitive_id": primitive_id,
                    "center_m": center,
                    "radius_m": radius,
                }
            )
    return collision_points, radii, runtime_spheres


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
    clearance_margin_by_type_m: dict[str, float] | None = None,
    maximum_collision_details: int = 128,
    tool_to_control: np.ndarray | None = None,
    effector_spheres: Iterable[dict[str, Any]] | None = None,
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
    collision_count = 0
    first_collision_sample_index: int | None = None
    configured_effector_spheres = tuple(effector_spheres or ())
    if scene is not None:
        for sample_index, q in enumerate(samples):
            collision_points, radii, runtime_spheres = (
                _configuration_collision_geometry(
                    kinematics,
                    q,
                    link_radii_m,
                    tool_to_control,
                    configured_effector_spheres,
                )
            )
            report = configuration_clearance(
                collision_points,
                scene,
                radii,
                robot_spheres=runtime_spheres,
                allowed_contact_object_ids=allowed_contact_object_ids,
                permit_pushable_contact=permit_pushable_contact,
                clearance_margin_by_type_m=clearance_margin_by_type_m,
                maximum_collision_details=max(
                    0,
                    int(maximum_collision_details) - len(collisions),
                ),
            )
            clearance = report["minimum_clearance_m"]
            if clearance is not None:
                minimum_clearance = clearance if minimum_clearance is None else min(minimum_clearance, clearance)
            collision_count += int(report.get("collision_count") or 0)
            if (
                first_collision_sample_index is None
                and int(report.get("collision_count") or 0) > 0
            ):
                first_collision_sample_index = sample_index
            collisions.extend({"sample_index": sample_index, **item} for item in report["collisions"])
    return PlanPreview(
        str(uuid.uuid4()),
        None if scene is None else scene.revision,
        float(duration_s),
        samples[0].copy(),
        samples[-1].copy(),
        tuple(samples),
        minimum_clearance,
        collision_count == 0,
        collision_count,
        first_collision_sample_index,
        tuple(collisions),
    )


def closest_collision_free_prefix(
    kinematics: ArmKinematics,
    preview: PlanPreview,
    *,
    scene: SceneSnapshot,
    link_radii_m: Iterable[float],
    allowed_contact_object_ids: set[str] | None = None,
    permit_pushable_contact: bool = False,
    clearance_margin_by_type_m: dict[str, float] | None = None,
    tool_to_control: np.ndarray | None = None,
    effector_spheres: Iterable[dict[str, Any]] | None = None,
    boundary_iterations: int = 24,
) -> tuple[np.ndarray, ...]:
    """Return the sampled path prefix immediately before first contact."""

    if preview.collision_free:
        return tuple(sample.copy() for sample in preview.samples)
    first_blocked = preview.first_collision_sample_index
    if first_blocked is None or first_blocked <= 0:
        return ()
    prefix = [sample.copy() for sample in preview.samples[:first_blocked]]
    low = prefix[-1].copy()
    high = preview.samples[first_blocked].copy()
    radii = tuple(float(value) for value in link_radii_m)
    configured_effector_spheres = tuple(effector_spheres or ())

    def collision_free(q: np.ndarray) -> bool:
        collision_points, checked_radii, runtime_spheres = (
            _configuration_collision_geometry(
                kinematics,
                q,
                radii,
                tool_to_control,
                configured_effector_spheres,
            )
        )
        report = configuration_clearance(
            collision_points,
            scene,
            checked_radii,
            robot_spheres=runtime_spheres,
            allowed_contact_object_ids=allowed_contact_object_ids,
            permit_pushable_contact=permit_pushable_contact,
            clearance_margin_by_type_m=clearance_margin_by_type_m,
            maximum_collision_details=0,
        )
        return bool(report["collision_free"])

    for _ in range(max(1, int(boundary_iterations))):
        middle = (low + high) * 0.5
        if collision_free(middle):
            low = middle
        else:
            high = middle
    if not np.allclose(prefix[-1], low, rtol=0.0, atol=1e-12):
        prefix.append(low)
    return tuple(prefix)


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
    clearance_margin_by_type_m: dict[str, float] | None = None,
    maximum_collision_details: int = 128,
    tool_to_control: np.ndarray | None = None,
    effector_spheres: Iterable[dict[str, Any]] | None = None,
) -> PlanPreview:
    segment = QuinticJointSegment.create(q_start, q_goal, duration_s)
    samples = tuple(item[0] for item in segment.sampled(sample_count))
    minimum_clearance: float | None = None
    collisions: list[dict[str, Any]] = []
    collision_count = 0
    first_collision_sample_index: int | None = None
    configured_effector_spheres = tuple(effector_spheres or ())
    if scene is not None:
        for sample_index, q in enumerate(samples):
            collision_points, radii, runtime_spheres = (
                _configuration_collision_geometry(
                    kinematics,
                    q,
                    link_radii_m,
                    tool_to_control,
                    configured_effector_spheres,
                )
            )
            report = configuration_clearance(
                collision_points,
                scene,
                radii,
                robot_spheres=runtime_spheres,
                allowed_contact_object_ids=allowed_contact_object_ids,
                permit_pushable_contact=permit_pushable_contact,
                clearance_margin_by_type_m=clearance_margin_by_type_m,
                maximum_collision_details=max(
                    0,
                    int(maximum_collision_details) - len(collisions),
                ),
            )
            clearance = report["minimum_clearance_m"]
            if clearance is not None:
                minimum_clearance = clearance if minimum_clearance is None else min(minimum_clearance, clearance)
            for collision in report["collisions"]:
                collisions.append({"sample_index": sample_index, **collision})
            collision_count += int(report.get("collision_count") or 0)
            if (
                first_collision_sample_index is None
                and int(report.get("collision_count") or 0) > 0
            ):
                first_collision_sample_index = sample_index
    return PlanPreview(
        str(uuid.uuid4()),
        None if scene is None else scene.revision,
        float(duration_s),
        samples[0].copy(),
        samples[-1].copy(),
        samples,
        minimum_clearance,
        collision_count == 0,
        collision_count,
        first_collision_sample_index,
        tuple(collisions),
    )
