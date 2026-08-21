from __future__ import annotations

from typing import Any
import math


SKILL_ID = "grip.grip_object"
EXTENSION_ID = "midbrain.provider.grip_control.v1"


def _vector(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must be finite")
    return result


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _unit(value: list[float], name: str) -> list[float]:
    norm = math.sqrt(_dot(value, value))
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError(f"{name} must be non-zero")
    return [component / norm for component in value]


def _orthogonal_pair(first: Any, second: Any, prefix: str) -> tuple[list[float], list[float]]:
    inward = _unit(_vector(first, f"{prefix} table inward direction"), f"{prefix} table inward direction")
    raw_insertion = _vector(second, f"{prefix} insertion direction")
    projected = [
        component - _dot(raw_insertion, inward) * inward[index]
        for index, component in enumerate(raw_insertion)
    ]
    return inward, _unit(projected, f"{prefix} projected insertion direction")


def _matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[row][k] * b[k][column] for k in range(3)) for column in range(3)] for row in range(3)]


def _transpose(value: list[list[float]]) -> list[list[float]]:
    return [[value[column][row] for column in range(3)] for row in range(3)]


def _basis(first: list[float], second: list[float]) -> list[list[float]]:
    third = _unit(_cross(first, second), "derived third direction")
    return [[first[row], second[row], third[row]] for row in range(3)]


def _quaternion_rotation(value: Any) -> list[list[float]]:
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


def _rpy(rotation: list[list[float]]) -> list[float]:
    pitch = math.asin(max(-1.0, min(1.0, -rotation[2][0])))
    if abs(math.cos(pitch)) > 1e-8:
        roll = math.atan2(rotation[2][1], rotation[2][2])
        yaw = math.atan2(rotation[1][0], rotation[0][0])
    else:
        roll = math.atan2(-rotation[1][2], rotation[1][1])
        yaw = 0.0
    return [roll, pitch, yaw]


def _quaternion(rotation: list[list[float]]) -> list[float]:
    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        return [
            (rotation[2][1] - rotation[1][2]) / scale,
            (rotation[0][2] - rotation[2][0]) / scale,
            (rotation[1][0] - rotation[0][1]) / scale,
            0.25 * scale,
        ]
    index = max(range(3), key=lambda item: rotation[item][item])
    if index == 0:
        scale = math.sqrt(1.0 + rotation[0][0] - rotation[1][1] - rotation[2][2]) * 2.0
        return [0.25 * scale, (rotation[0][1] + rotation[1][0]) / scale, (rotation[0][2] + rotation[2][0]) / scale, (rotation[2][1] - rotation[1][2]) / scale]
    if index == 1:
        scale = math.sqrt(1.0 + rotation[1][1] - rotation[0][0] - rotation[2][2]) * 2.0
        return [(rotation[0][1] + rotation[1][0]) / scale, 0.25 * scale, (rotation[1][2] + rotation[2][1]) / scale, (rotation[0][2] - rotation[2][0]) / scale]
    scale = math.sqrt(1.0 + rotation[2][2] - rotation[0][0] - rotation[1][1]) * 2.0
    return [(rotation[0][2] + rotation[2][0]) / scale, (rotation[1][2] + rotation[2][1]) / scale, 0.25 * scale, (rotation[1][0] - rotation[0][1]) / scale]


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3)]


def build_plan(
    arguments: dict[str, Any],
    *,
    effector_profile: dict[str, Any],
    gripper_vector_profile: dict[str, Any],
    motion_profile: dict[str, Any],
    world_from_base: dict[str, Any],
) -> dict[str, Any]:
    extension = (
        effector_profile
        if effector_profile.get("schema") == "midbrain.grip_effector_control_profile"
        else effector_profile.get("extensions", {}).get(EXTENSION_ID)
    )
    if not isinstance(extension, dict):
        raise RuntimeError("active effector has no compatible grip-control profile")
    effector_inward, effector_insertion = _orthogonal_pair(
        gripper_vector_profile["table_inward_direction_effector"],
        gripper_vector_profile["insertion_direction_effector"],
        "effector",
    )
    world_inward, world_insertion = _orthogonal_pair(
        arguments["table_inward_direction_world"],
        arguments["insertion_direction_world"],
        "object",
    )
    world_effector_rotation = _matmul(
        _basis(world_inward, world_insertion),
        _transpose(_basis(effector_inward, effector_insertion)),
    )
    world_base_rotation = _quaternion_rotation(world_from_base["rotation_xyzw"])
    base_world_rotation = _transpose(world_base_rotation)
    base_effector_rotation = _matmul(base_world_rotation, world_effector_rotation)
    orientation_xyzw = _quaternion(base_effector_rotation)
    begin_world = _vector(arguments["approach_begin_point_world_m"], "approach_begin_point_world_m")
    translation_world = _vector(world_from_base["translation_m"], "world_from_base.translation_m")
    begin_base = _matvec(
        base_world_rotation,
        [begin_world[index] - translation_world[index] for index in range(3)],
    )
    table_value = arguments.get("table_inward_distance_m")
    insertion_value = arguments.get("insertion_distance_m")
    table_distance = float(
        motion_profile["table_inward_distance_m"]
        if table_value is None
        else table_value
    )
    insertion_distance = float(
        motion_profile["insertion_distance_m"]
        if insertion_value is None
        else insertion_value
    )
    if not 0.0 < table_distance <= 0.25 or not 0.0 < insertion_distance <= 0.25:
        raise ValueError("grip approach distances must be in (0, 0.25] m")
    stage_waits = {
        "lower": float(motion_profile.get("delay_after_lower_s", 1.5)),
        "scrap": float(motion_profile.get("delay_after_scrap_s", 1.5)),
        "grip": float(motion_profile.get("delay_after_grip_s", 1.5)),
    }
    if any(
        not math.isfinite(value) or not 0.0 <= value <= 55.0
        for value in stage_waits.values()
    ):
        raise ValueError("scrap-grip stage delays must be in [0, 55] seconds")
    table_delta_base = _matvec(base_world_rotation, [component * table_distance for component in world_inward])
    insertion_delta_base = _matvec(base_world_rotation, [component * insertion_distance for component in world_insertion])
    torque_value = arguments.get("gripping_torque_limit_nm")
    torque = float(
        motion_profile["gripping_torque_limit_nm"]
        if torque_value is None
        else torque_value
    )
    joint = extension["joint_control"]
    if not 0.0 < torque <= float(joint["maximum_torque_limit_nm"]):
        raise ValueError("gripping_torque_limit_nm exceeds the effector profile")
    return {
        "approach_begin_point_base_m": begin_base,
        "target_orientation_rpy_rad": _rpy(base_effector_rotation),
        "orientation_xyzw": orientation_xyzw,
        "table_delta_base_m": table_delta_base,
        "insertion_delta_base_m": insertion_delta_base,
        "grip": {
            "position_rad": float(motion_profile.get("grip_position_rad", joint["default_grip_position_rad"])),
            "velocity_limit_rad_s": float(motion_profile.get("grip_velocity_rad_s", joint["default_velocity_rad_s"])),
            "torque_limit_nm": torque,
            "contact_timeout_s": float(motion_profile["contact_timeout_s"]),
        },
        "stage_waits_s": stage_waits,
        "release": {
            "position_rad": float(joint["open_position_rad"]),
            "position_tolerance_rad": float(joint["open_position_tolerance_rad"]),
            "velocity_limit_rad_s": float(joint["default_velocity_rad_s"]),
            "torque_limit_nm": float(joint["release_torque_limit_nm"]),
            "mit_delta_time_s": float(extension["mit_float_transition"]["default_delta_time_s"]),
        },
        "approach_open": {
            "position_rad": float(joint["open_position_rad"]),
            "position_tolerance_rad": float(joint["open_position_tolerance_rad"]),
            "duration_s": float(
                extension["mit_position_transition"]["default_duration_s"]
            ),
            "kp": float(extension["mit_position_transition"]["kp"]),
            "kd": float(extension["mit_position_transition"]["kd"]),
            "interpolation_rate_hz": float(
                extension["mit_position_transition"]["interpolation_rate_hz"]
            ),
            "readiness": "THERMAL_READY_AND_FUNCTIONALLY_OPEN_MIT_HOLD",
        },
        "attachment": {
            "object_binding": arguments["object_binding"],
            "payload": arguments.get("payload"),
            "collision_geometry": arguments.get("collision_geometry"),
        },
        "gripper_vector_profile": {
            "profile_number": int(gripper_vector_profile["profile_number"]),
            "name": str(gripper_vector_profile["name"]),
        },
        "construction": "ALIGN_BEGIN_WITH_INTEGRATED_THEN_TABLE_INWARD_THEN_INSERT_THEN_GRIP",
    }
