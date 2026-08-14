from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import math

import numpy as np


def rpy_matrix(rpy: Iterable[float]) -> np.ndarray:
    roll, pitch, yaw = (float(value) for value in rpy)
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


def axis_rotation(axis: Iterable[float], angle: float) -> np.ndarray:
    vector = np.asarray(list(axis), dtype=float)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("rotation axis must be non-zero")
    x, y, z = vector / norm
    c = math.cos(float(angle))
    s = math.sin(float(angle))
    one_minus_c = 1.0 - c
    return np.array(
        [
            [c + x * x * one_minus_c, x * y * one_minus_c - z * s, x * z * one_minus_c + y * s],
            [y * x * one_minus_c + z * s, c + y * y * one_minus_c, y * z * one_minus_c - x * s],
            [z * x * one_minus_c - y * s, z * y * one_minus_c + x * s, c + z * z * one_minus_c],
        ],
        dtype=float,
    )


def transform(translation: Iterable[float] = (0.0, 0.0, 0.0), rotation: np.ndarray | None = None) -> np.ndarray:
    result = np.eye(4, dtype=float)
    result[:3, 3] = np.asarray(list(translation), dtype=float)
    if rotation is not None:
        result[:3, :3] = np.asarray(rotation, dtype=float)
    return result


def matrix_rpy(rotation: np.ndarray) -> np.ndarray:
    """Return XYZ roll/pitch/yaw for an Rz(yaw) Ry(pitch) Rx(roll) matrix."""
    matrix = np.asarray(rotation, dtype=float)
    pitch = math.asin(float(np.clip(-matrix[2, 0], -1.0, 1.0)))
    cp = math.cos(pitch)
    if abs(cp) > 1e-8:
        roll = math.atan2(float(matrix[2, 1]), float(matrix[2, 2]))
        yaw = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    else:
        roll = 0.0
        yaw = math.atan2(float(-matrix[0, 1]), float(matrix[1, 1]))
    return np.array([roll, pitch, yaw], dtype=float)


def rotation_vector(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=float)
    trace = float(np.trace(matrix))
    cosine = float(np.clip((trace - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cosine)
    if angle < 1e-8:
        return 0.5 * np.array(
            [
                matrix[2, 1] - matrix[1, 2],
                matrix[0, 2] - matrix[2, 0],
                matrix[1, 0] - matrix[0, 1],
            ],
            dtype=float,
        )
    if math.pi - angle < 1e-5:
        diagonal = np.maximum((np.diag(matrix) + 1.0) * 0.5, 0.0)
        axis = np.sqrt(diagonal)
        if matrix[2, 1] - matrix[1, 2] < 0:
            axis[0] *= -1.0
        if matrix[0, 2] - matrix[2, 0] < 0:
            axis[1] *= -1.0
        if matrix[1, 0] - matrix[0, 1] < 0:
            axis[2] *= -1.0
        norm = float(np.linalg.norm(axis))
        axis = np.array([1.0, 0.0, 0.0], dtype=float) if norm <= 1e-9 else axis / norm
        return axis * angle
    scale = angle / (2.0 * math.sin(angle))
    return scale * np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ],
        dtype=float,
    )


@dataclass(frozen=True)
class FrameResult:
    transform: np.ndarray
    joint_origins: tuple[np.ndarray, ...]
    joint_axes: tuple[np.ndarray, ...]
    points: tuple[np.ndarray, ...]


@dataclass(frozen=True)
class PoseIkResult:
    q_goal: np.ndarray
    achieved_transform: np.ndarray
    position_residual_m: float
    orientation_residual_rad: float
    iterations: int
    sigma_min: float


class ArmKinematics:
    """Six-joint kinematics loaded directly from the Basic arm model."""

    def __init__(self, model: dict[str, Any]):
        joints = list(model.get("joints", []))[:6]
        if len(joints) != 6:
            raise ValueError("Basic Controller model must provide six arm joints")
        self.joints: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        limits: list[list[float]] = []
        for joint in joints:
            kinematics = dict(joint["kinematics"])
            axis = np.asarray(kinematics["axis_local"], dtype=float)
            axis_norm = float(np.linalg.norm(axis))
            if axis_norm <= 1e-12:
                raise ValueError(f"{joint.get('name', 'joint')} has a zero kinematic axis")
            self.joints.append(
                (
                    np.asarray(kinematics["origin_translation_m"], dtype=float),
                    rpy_matrix(kinematics["origin_rpy_rad"]),
                    axis / axis_norm,
                )
            )
            limits.append([float(value) for value in joint["operational_limit_rad"]])
        fixed = dict(model["fixed_tool"])
        self.fixed_tool = transform(fixed["translation_m"], rpy_matrix(fixed["rpy_rad"]))
        self.limits = np.asarray(limits, dtype=float)

    def evaluate(self, q: Iterable[float]) -> FrameResult:
        values = np.asarray(list(q), dtype=float)
        if values.shape != (6,):
            raise ValueError("q must contain six arm joint values")
        current = np.eye(4, dtype=float)
        origins: list[np.ndarray] = []
        axes: list[np.ndarray] = []
        points: list[np.ndarray] = [current[:3, 3].copy()]
        for index, (origin_xyz, origin_rotation, axis_local) in enumerate(self.joints):
            before_joint = current @ transform(origin_xyz, origin_rotation)
            origins.append(before_joint[:3, 3].copy())
            axes.append(before_joint[:3, :3] @ axis_local)
            current = before_joint @ transform(rotation=axis_rotation(axis_local, values[index]))
            points.append(current[:3, 3].copy())
        tool = current @ self.fixed_tool
        points.append(tool[:3, 3].copy())
        return FrameResult(tool, tuple(origins), tuple(axes), tuple(points))

    def controlled_frame(self, q: Iterable[float], tool_to_control: np.ndarray | None = None) -> np.ndarray:
        result = self.evaluate(q).transform
        if tool_to_control is None:
            return result.copy()
        offset = np.asarray(tool_to_control, dtype=float)
        if offset.shape != (4, 4) or not np.all(np.isfinite(offset)):
            raise ValueError("tool_to_control must be a finite 4x4 transform")
        return result @ offset

    def geometric_jacobian(
        self,
        q: Iterable[float],
        tool_to_control: np.ndarray | None = None,
    ) -> tuple[np.ndarray, FrameResult]:
        result = self.evaluate(q)
        controlled = self.controlled_frame(q, tool_to_control)
        point = controlled[:3, 3]
        jacobian = np.zeros((6, 6), dtype=float)
        for index, (origin, axis) in enumerate(zip(result.joint_origins, result.joint_axes)):
            jacobian[:3, index] = np.cross(axis, point - origin)
            jacobian[3:, index] = axis
        controlled_result = FrameResult(controlled, result.joint_origins, result.joint_axes, result.points)
        return jacobian, controlled_result

    def solve_weighted_pose(
        self,
        q_seed: Iterable[float],
        target_transform: np.ndarray,
        *,
        position_tolerance_m: float,
        orientation_tolerance_rad: float,
        maximum_iterations: int,
        damping: float,
        maximum_step_rad: float,
        joint_margin_rad: float,
        orientation_weight_m_per_rad: float,
        orientation_required: bool,
        tool_to_control: np.ndarray | None = None,
    ) -> PoseIkResult:
        """Solve a Cartesian target with translation priority.

        When orientation_required is false, orientation remains a soft secondary
        objective and convergence is determined by Cartesian position. This is
        intentionally more tolerant of small physical/model orientation errors.
        """
        q = np.asarray(list(q_seed), dtype=float).copy()
        target = np.asarray(target_transform, dtype=float)
        if q.shape != (6,):
            raise ValueError("q_seed must contain six values")
        if target.shape != (4, 4) or not np.all(np.isfinite(target)):
            raise ValueError("target_transform must be a finite 4x4 matrix")
        lower = self.limits[:, 0] + float(joint_margin_rad)
        upper = self.limits[:, 1] - float(joint_margin_rad)
        if np.any(lower >= upper):
            raise ValueError("joint margin leaves no operational range")
        q = np.clip(q, lower, upper)
        weight = max(0.0, float(orientation_weight_m_per_rad))
        sigma_min = 0.0

        for iteration in range(1, int(maximum_iterations) + 1):
            jacobian, frame = self.geometric_jacobian(q, tool_to_control)
            current = frame.transform
            position_error = target[:3, 3] - current[:3, 3]
            orientation_error = rotation_vector(target[:3, :3] @ current[:3, :3].T)
            position_residual = float(np.linalg.norm(position_error))
            orientation_residual = float(np.linalg.norm(orientation_error))
            if orientation_required:
                effective_jacobian = jacobian.copy()
                effective_jacobian[3:, :] *= weight
            else:
                effective_jacobian = jacobian[:3, :]
            singular_values = np.linalg.svd(
                effective_jacobian,
                compute_uv=False,
            )
            sigma_min = (
                float(singular_values[-1])
                if singular_values.size
                else 0.0
            )
            orientation_ok = orientation_residual <= float(orientation_tolerance_rad)
            if position_residual <= float(position_tolerance_m) and (orientation_ok or not orientation_required):
                return PoseIkResult(q.copy(), current.copy(), position_residual, orientation_residual, iteration, sigma_min)

            if orientation_required:
                error = np.concatenate([position_error, orientation_error * weight])
                adaptive = float(damping) + max(0.0, 0.02 - sigma_min) * 2.0
                gram = effective_jacobian @ effective_jacobian.T + (adaptive**2) * np.eye(6, dtype=float)
                delta = effective_jacobian.T @ np.linalg.solve(gram, error)
            else:
                # Translation-only input gets a true 3x6 redundant IK solve.
                # This deliberately avoids spending all six DoF on matching a
                # possibly imperfect model orientation during physical bring-up.
                adaptive = float(damping) + max(0.0, 0.02 - sigma_min) * 2.0
                gram = effective_jacobian @ effective_jacobian.T + (adaptive**2) * np.eye(3, dtype=float)
                delta = effective_jacobian.T @ np.linalg.solve(gram, position_error)
            largest = float(np.max(np.abs(delta)))
            if largest > float(maximum_step_rad):
                delta *= float(maximum_step_rad) / largest
            q = np.clip(q + delta, lower, upper)

        final = self.controlled_frame(q, tool_to_control)
        return PoseIkResult(
            q.copy(),
            final.copy(),
            float(np.linalg.norm(target[:3, 3] - final[:3, 3])),
            float(np.linalg.norm(rotation_vector(target[:3, :3] @ final[:3, :3].T))),
            int(maximum_iterations),
            sigma_min,
        )
