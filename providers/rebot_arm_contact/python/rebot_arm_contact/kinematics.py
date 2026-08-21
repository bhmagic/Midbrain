from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import math

import numpy as np


def rpy_matrix(values: Iterable[float]) -> np.ndarray:
    roll, pitch, yaw = [float(value) for value in values]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )


def quaternion_matrix(values: Iterable[float]) -> np.ndarray:
    x, y, z, w = [float(value) for value in values]
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("orientation quaternion must have non-zero finite norm")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=float,
    )


def matrix_quaternion(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        result = [
            (matrix[2, 1] - matrix[1, 2]) / scale,
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[1, 0] - matrix[0, 1]) / scale,
            0.25 * scale,
        ]
    else:
        index = int(np.argmax(np.diag(matrix)))
        if index == 0:
            scale = math.sqrt(
                1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]
            ) * 2.0
            result = [
                0.25 * scale,
                (matrix[0, 1] + matrix[1, 0]) / scale,
                (matrix[0, 2] + matrix[2, 0]) / scale,
                (matrix[2, 1] - matrix[1, 2]) / scale,
            ]
        elif index == 1:
            scale = math.sqrt(
                1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]
            ) * 2.0
            result = [
                (matrix[0, 1] + matrix[1, 0]) / scale,
                0.25 * scale,
                (matrix[1, 2] + matrix[2, 1]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
            ]
        else:
            scale = math.sqrt(
                1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]
            ) * 2.0
            result = [
                (matrix[0, 2] + matrix[2, 0]) / scale,
                (matrix[1, 2] + matrix[2, 1]) / scale,
                0.25 * scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
            ]
    norm = math.sqrt(sum(float(value) ** 2 for value in result))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("rotation could not be converted to a quaternion")
    return [float(value) / norm for value in result]


def axis_rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    one_minus_c = 1.0 - c
    return np.array(
        [
            [c + x * x * one_minus_c, x * y * one_minus_c - z * s, x * z * one_minus_c + y * s],
            [y * x * one_minus_c + z * s, c + y * y * one_minus_c, y * z * one_minus_c - x * s],
            [z * x * one_minus_c - y * s, z * y * one_minus_c + x * s, c + z * z * one_minus_c],
        ],
        dtype=float,
    )


def transform(
    translation: Iterable[float] = (0.0, 0.0, 0.0),
    rotation: np.ndarray | None = None,
) -> np.ndarray:
    result = np.eye(4, dtype=float)
    result[:3, 3] = np.asarray(list(translation), dtype=float)
    if rotation is not None:
        result[:3, :3] = np.asarray(rotation, dtype=float)
    return result


def rotation_vector(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=float)
    cosine = float(np.clip((float(np.trace(matrix)) - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cosine)
    skew = np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ],
        dtype=float,
    )
    if angle < 1e-8:
        return 0.5 * skew
    if abs(math.sin(angle)) < 1e-7:
        diagonal = np.maximum((np.diag(matrix) + 1.0) * 0.5, 0.0)
        axis = np.sqrt(diagonal)
        if float(np.linalg.norm(axis)) <= 1e-9:
            axis = np.array([1.0, 0.0, 0.0], dtype=float)
        else:
            axis /= float(np.linalg.norm(axis))
        return axis * angle
    return skew * (angle / (2.0 * math.sin(angle)))


@dataclass(frozen=True)
class KinematicState:
    controlled_transform: np.ndarray
    joint_origins: tuple[np.ndarray, ...]
    joint_axes: tuple[np.ndarray, ...]


@dataclass(frozen=True)
class IkResult:
    q_goal: np.ndarray
    position_residual_m: float
    orientation_residual_rad: float
    iterations: int
    sigma_min: float


class ContactKinematics:
    """Independent six-joint FK, locked-joint IK, and acting-point Jacobian."""

    def __init__(self, model: dict[str, Any], tool_to_control: np.ndarray):
        joints = list(model.get("joints", []))[:6]
        if len(joints) != 6:
            raise ValueError("Basic model must expose six arm joints")
        self.joint_names = [str(joint["name"]) for joint in joints]
        self.root_frame_id = str(
            model.get("coordinate_convention", {}).get("root_frame")
            or "rebot_arm_base"
        )
        self.joints: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        limits: list[list[float]] = []
        for joint in joints:
            data = dict(joint["kinematics"])
            axis = np.asarray(data["axis_local"], dtype=float)
            axis /= float(np.linalg.norm(axis))
            self.joints.append(
                (
                    np.asarray(data["origin_translation_m"], dtype=float),
                    rpy_matrix(data["origin_rpy_rad"]),
                    axis,
                )
            )
            limits.append([float(value) for value in joint["operational_limit_rad"]])
        fixed = dict(model["fixed_tool"])
        self.fixed_tool = transform(
            fixed["translation_m"], rpy_matrix(fixed["rpy_rad"])
        )
        offset = np.asarray(tool_to_control, dtype=float)
        if offset.shape != (4, 4) or not np.all(np.isfinite(offset)):
            raise ValueError("tool_to_control must be a finite 4x4 transform")
        self.tool_to_control = offset
        self.limits = np.asarray(limits, dtype=float)

    def evaluate(self, q: Iterable[float]) -> KinematicState:
        values = np.asarray(list(q), dtype=float)
        if values.shape != (6,) or not np.all(np.isfinite(values)):
            raise ValueError("q must contain six finite values")
        current = np.eye(4, dtype=float)
        origins: list[np.ndarray] = []
        axes: list[np.ndarray] = []
        for index, (origin, origin_rotation, axis_local) in enumerate(self.joints):
            before = current @ transform(origin, origin_rotation)
            origins.append(before[:3, 3].copy())
            axes.append(before[:3, :3] @ axis_local)
            current = before @ transform(
                rotation=axis_rotation(axis_local, float(values[index]))
            )
        controlled = current @ self.fixed_tool @ self.tool_to_control
        return KinematicState(controlled, tuple(origins), tuple(axes))

    def jacobian(self, q: Iterable[float]) -> tuple[np.ndarray, KinematicState]:
        state = self.evaluate(q)
        point = state.controlled_transform[:3, 3]
        jacobian = np.zeros((6, 6), dtype=float)
        for index, (origin, axis) in enumerate(
            zip(state.joint_origins, state.joint_axes)
        ):
            jacobian[:3, index] = np.cross(axis, point - origin)
            jacobian[3:, index] = axis
        return jacobian, state

    def target_transform(self, target: dict[str, Any]) -> np.ndarray:
        position = np.asarray(target["position_m"], dtype=float)
        quaternion = np.asarray(target["orientation_xyzw"], dtype=float)
        if position.shape != (3,) or quaternion.shape != (4,):
            raise ValueError("target position and orientation dimensions are invalid")
        if not np.all(np.isfinite(position)) or not np.all(np.isfinite(quaternion)):
            raise ValueError("target pose must be finite")
        return transform(position, quaternion_matrix(quaternion))

    def solve_pose(
        self,
        q_seed: Iterable[float],
        target: dict[str, Any],
        locked_positions: dict[int, float],
        *,
        maximum_iterations: int,
        damping: float,
        maximum_step_rad: float,
        joint_margin_rad: float,
        orientation_weight_m_per_rad: float,
    ) -> IkResult:
        q = np.asarray(list(q_seed), dtype=float).copy()
        if q.shape != (6,) or not np.all(np.isfinite(q)):
            raise ValueError("q_seed must contain six finite values")
        goal = self.target_transform(target)
        lower = self.limits[:, 0] + float(joint_margin_rad)
        upper = self.limits[:, 1] - float(joint_margin_rad)
        if np.any(lower >= upper):
            raise ValueError("joint margin leaves no operational range")
        for index, position in locked_positions.items():
            if not lower[index] <= position <= upper[index]:
                raise ValueError(f"locked joint {index} is outside operational limits")
            q[index] = float(position)
        active = np.array(
            [index for index in range(6) if index not in locked_positions],
            dtype=int,
        )
        q = np.clip(q, lower, upper)
        for index, position in locked_positions.items():
            q[index] = float(position)
        sigma_min = 0.0
        iteration = 0
        weight = float(orientation_weight_m_per_rad)
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("orientation_weight_m_per_rad must be positive")
        base_damping = float(damping)
        if not math.isfinite(base_damping) or base_damping <= 0.0:
            raise ValueError("damping must be positive")
        adaptive_damping = base_damping

        def residual(
            values: np.ndarray,
        ) -> tuple[np.ndarray, KinematicState, np.ndarray, np.ndarray, float]:
            jacobian_value, state_value = self.jacobian(values)
            position_error_value = (
                goal[:3, 3] - state_value.controlled_transform[:3, 3]
            )
            orientation_error_value = rotation_vector(
                goal[:3, :3] @ state_value.controlled_transform[:3, :3].T
            )
            scaled_error = np.concatenate(
                [position_error_value, orientation_error_value * weight]
            )
            return (
                jacobian_value,
                state_value,
                position_error_value,
                orientation_error_value,
                float(np.dot(scaled_error, scaled_error)),
            )

        (
            jacobian,
            _state,
            position_error,
            orientation_error,
            objective,
        ) = residual(q)
        best_q = q.copy()
        best_objective = objective
        for iteration in range(1, int(maximum_iterations) + 1):
            if (
                float(np.linalg.norm(position_error)) <= 1e-5
                and float(np.linalg.norm(orientation_error)) <= 1e-4
            ):
                break
            if active.size == 0:
                break
            weighted = jacobian[:, active].copy()
            weighted[3:, :] *= weight
            error = np.concatenate([position_error, orientation_error * weight])
            singular_values = np.linalg.svd(weighted, compute_uv=False)
            sigma_min = float(singular_values[-1]) if singular_values.size else 0.0
            gram = weighted @ weighted.T + adaptive_damping**2 * np.eye(6)
            delta = weighted.T @ np.linalg.solve(gram, error)
            largest = float(np.max(np.abs(delta))) if delta.size else 0.0
            if largest > float(maximum_step_rad):
                delta *= float(maximum_step_rad) / largest
            accepted = False
            for scale in (1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125):
                candidate = q.copy()
                candidate[active] = np.clip(
                    q[active] + scale * delta,
                    lower[active],
                    upper[active],
                )
                for index, position in locked_positions.items():
                    candidate[index] = float(position)
                candidate_result = residual(candidate)
                candidate_objective = candidate_result[4]
                if candidate_objective < objective - 1e-14:
                    q = candidate
                    (
                        jacobian,
                        _state,
                        position_error,
                        orientation_error,
                        objective,
                    ) = candidate_result
                    accepted = True
                    adaptive_damping = max(
                        base_damping,
                        adaptive_damping * 0.5,
                    )
                    if objective < best_objective:
                        best_q = q.copy()
                        best_objective = objective
                    break
            if not accepted:
                adaptive_damping *= 4.0
                if adaptive_damping > 1e4:
                    break
        final = self.evaluate(best_q).controlled_transform
        return IkResult(
            best_q,
            float(np.linalg.norm(goal[:3, 3] - final[:3, 3])),
            float(np.linalg.norm(rotation_vector(goal[:3, :3] @ final[:3, :3].T))),
            iteration,
            sigma_min,
        )

    def joint_wrench(
        self,
        q: Iterable[float],
        force_n: Iterable[float],
        torque_nm: Iterable[float],
        wrench_frame_id: str,
        acting_frame_id: str,
    ) -> np.ndarray:
        jacobian, state = self.jacobian(q)
        force = np.asarray(list(force_n), dtype=float)
        torque = np.asarray(list(torque_nm), dtype=float)
        if force.shape != (3,) or torque.shape != (3,):
            raise ValueError("force and torque must each contain three values")
        if not np.all(np.isfinite(force)) or not np.all(np.isfinite(torque)):
            raise ValueError("wrench must be finite")
        if wrench_frame_id == self.root_frame_id:
            rotation = np.eye(3, dtype=float)
        elif wrench_frame_id == acting_frame_id:
            rotation = state.controlled_transform[:3, :3]
        else:
            raise ValueError(
                "wrench frame must be the arm root or selected acting frame"
            )
        wrench_base = np.concatenate([rotation @ force, rotation @ torque])
        return jacobian.T @ wrench_base
