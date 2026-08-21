from __future__ import annotations

from typing import Any
import math


ABSOLUTE_WORLD_POINT_MODE = "ABSOLUTE_WORLD"
RELATIVE_WORLD_POINT_MODE = "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"


def normalize_point_mode(value: Any) -> str:
    mode = str(value or RELATIVE_WORLD_POINT_MODE).strip().upper()
    if mode not in {ABSOLUTE_WORLD_POINT_MODE, RELATIVE_WORLD_POINT_MODE}:
        raise ValueError(
            "point_mode must be ABSOLUTE_WORLD or "
            "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
        )
    return mode


def finite_vector3(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _rotation_from_quaternion(value: Any) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("world_from_base rotation must be a quaternion")
    x, y, z, w = (float(component) for component in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError("world_from_base quaternion is invalid")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    ]


def _transpose(matrix: list[list[float]]) -> list[list[float]]:
    return [[matrix[column][row] for column in range(3)] for row in range(3)]


def _transform_parts(
    world_from_base: dict[str, Any],
) -> tuple[list[list[float]], list[float]]:
    if not isinstance(world_from_base, dict):
        raise ValueError("world_from_base must be an object")
    world_base_rotation = _rotation_from_quaternion(
        world_from_base.get("rotation_xyzw")
    )
    translation_world = finite_vector3(
        world_from_base.get("translation_m"),
        "world_from_base.translation_m",
    )
    return world_base_rotation, translation_world


def world_offset_to_base(
    offset_world_m: Any,
    world_from_base: dict[str, Any],
) -> list[float]:
    offset = finite_vector3(offset_world_m, "approach_begin_offset_world_m")
    world_base_rotation, _translation = _transform_parts(world_from_base)
    return _matvec(_transpose(world_base_rotation), offset)


def world_point_to_base(
    point_world_m: Any,
    world_from_base: dict[str, Any],
) -> list[float]:
    point = finite_vector3(point_world_m, "approach_begin_point_world_m")
    world_base_rotation, translation = _transform_parts(world_from_base)
    return _matvec(
        _transpose(world_base_rotation),
        [point[index] - translation[index] for index in range(3)],
    )


def base_point_to_world(
    point_base_m: Any,
    world_from_base: dict[str, Any],
) -> list[float]:
    point = finite_vector3(point_base_m, "current_effector_base_m")
    world_base_rotation, translation = _transform_parts(world_from_base)
    rotated = _matvec(world_base_rotation, point)
    return [rotated[index] + translation[index] for index in range(3)]


def resolve_approach_point(
    entered_world_m: Any,
    *,
    point_mode: Any,
    world_from_base: dict[str, Any],
    current_effector_base_m: Any | None = None,
) -> dict[str, Any]:
    mode = normalize_point_mode(point_mode)
    entered = finite_vector3(
        entered_world_m,
        "approach_begin_point_world_m",
    )
    if mode == ABSOLUTE_WORLD_POINT_MODE:
        return {
            "point_mode": mode,
            "entered_world_m": entered,
            "captured_current_effector_base_m": None,
            "captured_current_effector_world_m": None,
            "resolved_approach_begin_point_world_m": entered,
            "resolved_approach_begin_point_base_m": world_point_to_base(
                entered,
                world_from_base,
            ),
            "origin_capture_policy": "NOT_APPLICABLE_ABSOLUTE_WORLD",
        }
    if current_effector_base_m is None:
        raise RuntimeError(
            "relative approach-begin entry requires a measured current effector position"
        )
    current_base = finite_vector3(
        current_effector_base_m,
        "current_effector_base_m",
    )
    current_world = base_point_to_world(current_base, world_from_base)
    offset_base = world_offset_to_base(entered, world_from_base)
    resolved_world = [
        current_world[index] + entered[index] for index in range(3)
    ]
    return {
        "point_mode": mode,
        "entered_world_m": entered,
        "captured_current_effector_base_m": current_base,
        "captured_current_effector_world_m": current_world,
        "resolved_approach_begin_point_world_m": resolved_world,
        "resolved_approach_begin_point_base_m": [
            current_base[index] + offset_base[index]
            for index in range(3)
        ],
        "origin_capture_policy": "CAPTURED_DURING_PREPARATION",
    }
