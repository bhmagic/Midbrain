from __future__ import annotations

from typing import Any
import math


def _vector(value: Any, name: str, length: int = 3) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain {length} values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must be finite")
    return result


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _unit(value, name):
    norm = math.sqrt(_dot(value, value))
    if norm < 1e-8:
        raise ValueError(f"{name} must be non-zero")
    return [component / norm for component in value]


def _pair(first, second, prefix):
    first = _unit(_vector(first, f"{prefix} first direction"), f"{prefix} first direction")
    second = _vector(second, f"{prefix} second direction")
    second = [value - _dot(second, first) * first[index] for index, value in enumerate(second)]
    return first, _unit(second, f"{prefix} projected second direction")


def _basis(first, second):
    third = _unit(_cross(first, second), "derived third direction")
    return [[first[row], second[row], third[row]] for row in range(3)]


def _transpose(value):
    return [[value[column][row] for column in range(3)] for row in range(3)]


def _matmul(a, b):
    return [[sum(a[row][k] * b[k][column] for k in range(3)) for column in range(3)] for row in range(3)]


def _rotation_from_quaternion(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("world_from_base rotation must be a quaternion")
    x, y, z, w = (float(component) for component in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-8:
        raise ValueError("world_from_base quaternion is invalid")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _quaternion(rotation):
    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        return [(rotation[2][1] - rotation[1][2]) / scale, (rotation[0][2] - rotation[2][0]) / scale, (rotation[1][0] - rotation[0][1]) / scale, 0.25 * scale]
    index = max(range(3), key=lambda item: rotation[item][item])
    if index == 0:
        scale = math.sqrt(1.0 + rotation[0][0] - rotation[1][1] - rotation[2][2]) * 2.0
        return [0.25 * scale, (rotation[0][1] + rotation[1][0]) / scale, (rotation[0][2] + rotation[2][0]) / scale, (rotation[2][1] - rotation[1][2]) / scale]
    if index == 1:
        scale = math.sqrt(1.0 + rotation[1][1] - rotation[0][0] - rotation[2][2]) * 2.0
        return [(rotation[0][1] + rotation[1][0]) / scale, 0.25 * scale, (rotation[1][2] + rotation[2][1]) / scale, (rotation[0][2] - rotation[2][0]) / scale]
    scale = math.sqrt(1.0 + rotation[2][2] - rotation[0][0] - rotation[1][1]) * 2.0
    return [(rotation[0][2] + rotation[2][0]) / scale, (rotation[1][2] + rotation[2][1]) / scale, 0.25 * scale, (rotation[1][0] - rotation[0][1]) / scale]


def quaternion_rpy(value: Any) -> list[float]:
    quaternion = _vector(value, "orientation quaternion", 4)
    norm = math.sqrt(sum(component * component for component in quaternion))
    if norm < 1e-8:
        raise ValueError("orientation quaternion must be nonzero")
    x, y, z, w = (component / norm for component in quaternion)
    return [
        math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)),
        math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))),
        math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)),
    ]


def two_vector_orientation(
    *,
    effector_first: Any,
    effector_second: Any,
    world_first: Any,
    world_second: Any,
    world_from_base_quaternion: Any,
) -> dict[str, Any]:
    effector_pair = _pair(effector_first, effector_second, "effector")
    world_pair = _pair(world_first, world_second, "world")
    world_effector = _matmul(_basis(*world_pair), _transpose(_basis(*effector_pair)))
    base_world = _transpose(_rotation_from_quaternion(world_from_base_quaternion))
    base_effector = _matmul(base_world, world_effector)
    return {
        "orientation_arm_base_xyzw": _quaternion(base_effector),
        "world_first": world_pair[0],
        "world_second": world_pair[1],
        "base_world_rotation": base_world,
    }
