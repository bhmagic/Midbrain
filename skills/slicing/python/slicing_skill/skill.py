from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import copy
import math

import numpy as np

from contact_work_runtime import ContactStep


SKILL_ID = "contact.slicing"
KGF_TO_NEWTON = 9.80665
BLADE_PROFILE_EXTENSION_ID = "midbrain.skill.slicing_blade_profiles.v1"
MOTION_PROFILES_SCHEMA = "midbrain.slicing_motion_profiles"
ABSOLUTE_WORLD_POINT_MODE = "ABSOLUTE_WORLD"
RELATIVE_WORLD_POINT_MODE = "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
BEGIN_POINT_FIELD = "slice_begin_point_m"
_DEGENERATE_TOLERANCE = 1e-9
_TRANSFORM_TOLERANCE = 1e-6


def _vector3(values: Iterable[float], name: str) -> np.ndarray:
    try:
        vector = np.asarray(tuple(float(value) for value in values), dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain three finite numbers") from exc
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must contain three finite numbers")
    return vector


def _unit(vector: np.ndarray, name: str) -> np.ndarray:
    magnitude = float(np.linalg.norm(vector))
    if not math.isfinite(magnitude) or magnitude <= _DEGENERATE_TOLERANCE:
        raise ValueError(f"{name} must be nonzero")
    return vector / magnitude


def _projected_unit(
    vector: np.ndarray,
    normal: np.ndarray,
    name: str,
) -> np.ndarray:
    projected = vector - float(np.dot(vector, normal)) * normal
    magnitude = float(np.linalg.norm(projected))
    if not math.isfinite(magnitude) or magnitude <= _DEGENERATE_TOLERANCE:
        raise ValueError(f"{name} is parallel to the priority blade direction")
    return projected / magnitude


def _rotation_from_quaternion_xyzw(values: Iterable[float]) -> np.ndarray:
    quaternion = np.asarray(tuple(float(value) for value in values), dtype=float)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("world_from_base.rotation_xyzw must contain four finite numbers")
    magnitude = float(np.linalg.norm(quaternion))
    if magnitude <= _DEGENERATE_TOLERANCE:
        raise ValueError("world_from_base.rotation_xyzw must be nonzero")
    x, y, z, w = quaternion / magnitude
    return np.asarray(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=float,
    )


def _quaternion_xyzw_from_rotation(rotation: np.ndarray) -> tuple[float, ...]:
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (rotation[2, 1] - rotation[1, 2]) / scale
        y = (rotation[0, 2] - rotation[2, 0]) / scale
        z = (rotation[1, 0] - rotation[0, 1]) / scale
    else:
        diagonal = np.diag(rotation)
        index = int(np.argmax(diagonal))
        if index == 0:
            scale = math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
            w = (rotation[2, 1] - rotation[1, 2]) / scale
            x = 0.25 * scale
            y = (rotation[0, 1] + rotation[1, 0]) / scale
            z = (rotation[0, 2] + rotation[2, 0]) / scale
        elif index == 1:
            scale = math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
            w = (rotation[0, 2] - rotation[2, 0]) / scale
            x = (rotation[0, 1] + rotation[1, 0]) / scale
            y = 0.25 * scale
            z = (rotation[1, 2] + rotation[2, 1]) / scale
        else:
            scale = math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
            w = (rotation[1, 0] - rotation[0, 1]) / scale
            x = (rotation[0, 2] + rotation[2, 0]) / scale
            y = (rotation[1, 2] + rotation[2, 1]) / scale
            z = 0.25 * scale
    quaternion = np.asarray([x, y, z, w], dtype=float)
    quaternion /= float(np.linalg.norm(quaternion))
    if quaternion[3] < 0.0:
        quaternion *= -1.0
    return tuple(float(value) for value in quaternion)


def _rpy_from_rotation(rotation: np.ndarray) -> tuple[float, float, float]:
    sine_pitch = float(np.clip(-rotation[2, 0], -1.0, 1.0))
    pitch = math.asin(sine_pitch)
    cosine_pitch = math.cos(pitch)
    if abs(cosine_pitch) > 1e-8:
        roll = math.atan2(rotation[2, 1], rotation[2, 2])
        yaw = math.atan2(rotation[1, 0], rotation[0, 0])
    else:
        roll = 0.0
        yaw = math.atan2(-rotation[0, 1], rotation[1, 1])
    return float(roll), float(pitch), float(yaw)


def _rotation_quality(rotation: np.ndarray) -> None:
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise ValueError("computed slicing rotation is invalid")
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-7, rtol=0.0):
        raise ValueError("computed slicing rotation is not orthonormal")
    if not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-7):
        raise ValueError("computed slicing rotation is not right-handed")


def _positive_profile_number(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if number <= 0 or number != value:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _locked_joint_names(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    names = tuple(str(item).strip() for item in value)
    if (
        len(names) > 6
        or any(not item or len(item) > 120 for item in names)
        or len(names) != len(set(names))
    ):
        raise ValueError(f"{name} must contain at most six unique joint names")
    return names


def blade_profiles_from_effector(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema") != "midbrain.mounted_effector_profile":
        raise ValueError("the active mounted-effector profile schema is unsupported")
    extensions = profile.get("extensions")
    extension = (
        extensions.get(BLADE_PROFILE_EXTENSION_ID)
        if isinstance(extensions, dict)
        else None
    )
    if not isinstance(extension, dict):
        raise ValueError(
            "the active mounted effector has no Slicing blade-profile extension"
        )
    if (
        extension.get("schema") != "midbrain.effector_slicing_blade_profiles"
        or extension.get("schema_version") != 1
    ):
        raise ValueError("the active Slicing blade-profile extension is unsupported")
    profiles = extension.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("the mounted-effector blade profiles must be an array")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for value in profiles:
        if not isinstance(value, dict):
            raise ValueError("a blade-use profile is not an object")
        number = _positive_profile_number(
            value.get("profile_number"), "blade profile_number"
        )
        if number in seen:
            raise ValueError(f"duplicate blade profile_number: {number}")
        seen.add(number)
        name = str(value.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ValueError(f"blade profile #{number} has an invalid name")
        blade = _unit(
            _vector3(
                value.get("blade_direction_effector", ()),
                f"blade profile #{number} blade_direction_effector",
            ),
            f"blade profile #{number} blade_direction_effector",
        )
        slicing = _projected_unit(
            _vector3(
                value.get("slicing_direction_effector", ()),
                f"blade profile #{number} slicing_direction_effector",
            ),
            blade,
            f"blade profile #{number} slicing_direction_effector",
        )
        locked = _locked_joint_names(
            value.get("locked_joint_names", []),
            f"blade profile #{number} locked_joint_names",
        )
        normalized.append(
            {
                "profile_number": number,
                "name": name,
                "blade_direction_effector": [
                    float(component)
                    for component in value["blade_direction_effector"]
                ],
                "slicing_direction_effector": [
                    float(component)
                    for component in value["slicing_direction_effector"]
                ],
                "projected_slicing_direction_effector": slicing.tolist(),
                "locked_joint_names": list(locked),
            }
        )
    default_value = extension.get("default_profile_number")
    if normalized:
        default_number = _positive_profile_number(
            default_value,
            "default blade profile number",
        )
        if default_number not in seen:
            raise ValueError("default blade profile number does not exist")
    else:
        if default_value is not None:
            raise ValueError("an empty blade-profile set must have no default")
        default_number = None
    normalized.sort(key=lambda item: item["profile_number"])
    return {
        "schema": extension["schema"],
        "schema_version": 1,
        "default_profile_number": default_number,
        "profiles": normalized,
    }


def motion_profiles_from_document(document: dict[str, Any]) -> dict[str, Any]:
    if (
        document.get("schema") != MOTION_PROFILES_SCHEMA
        or document.get("schema_version") != 1
    ):
        raise ValueError("the Slicing motion-profile configuration is unsupported")
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("the Slicing motion profiles must be an array")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    numeric_fields = (
        "blade_load_kgf",
        "retract_distance_m",
        "delay_after_engage_s",
        "slice_wait_speed_m_s",
        "delay_after_retract_s",
    )
    for value in profiles:
        if not isinstance(value, dict):
            raise ValueError("a Slicing motion profile is not an object")
        number = _positive_profile_number(
            value.get("profile_number"), "motion profile_number"
        )
        if number in seen:
            raise ValueError(f"duplicate motion profile_number: {number}")
        seen.add(number)
        name = str(value.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ValueError(f"motion profile #{number} has an invalid name")
        resolved: dict[str, float] = {}
        for field in numeric_fields:
            try:
                resolved[field] = float(value[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"motion profile #{number} has an invalid {field}"
                ) from exc
            if not math.isfinite(resolved[field]):
                raise ValueError(
                    f"motion profile #{number} has an invalid {field}"
                )
        if resolved["blade_load_kgf"] <= 0.0:
            raise ValueError("blade_load_kgf must be positive")
        if resolved["retract_distance_m"] <= 0.0:
            raise ValueError("retract_distance_m must be positive")
        if resolved["slice_wait_speed_m_s"] <= 0.0:
            raise ValueError("slice_wait_speed_m_s must be positive")
        for field in ("delay_after_engage_s", "delay_after_retract_s"):
            if not 0.0 <= resolved[field] <= 55.0:
                raise ValueError(f"{field} must be in [0, 55] seconds")
        normalized.append(
            {"profile_number": number, "name": name, **resolved}
        )
    default_value = document.get("default_profile_number")
    if normalized:
        default_number = _positive_profile_number(
            default_value,
            "default motion profile number",
        )
        if default_number not in seen:
            raise ValueError("default motion profile number does not exist")
    else:
        if default_value is not None:
            raise ValueError("an empty motion-profile set must have no default")
        default_number = None
    normalized.sort(key=lambda item: item["profile_number"])
    return {
        "schema": MOTION_PROFILES_SCHEMA,
        "schema_version": 1,
        "default_profile_number": default_number,
        "profiles": normalized,
    }


def _profile_by_number(
    document: dict[str, Any],
    number: Any,
    *,
    kind: str,
) -> dict[str, Any]:
    selected_number = _positive_profile_number(number, f"{kind}_profile_number")
    selected = next(
        (
            item
            for item in document["profiles"]
            if item["profile_number"] == selected_number
        ),
        None,
    )
    if selected is None:
        raise ValueError(f"unknown {kind} profile number: {selected_number}")
    return copy.deepcopy(selected)


def _blade_use(
    arguments: dict[str, Any],
    effector_profile: dict[str, Any] | None,
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...], int | None, str]:
    blade_explicit = arguments.get("blade_direction_effector")
    slicing_explicit = arguments.get("slicing_direction_effector")
    if blade_explicit is not None or slicing_explicit is not None:
        if blade_explicit is None or slicing_explicit is None:
            raise ValueError("both developer effector vectors must be supplied")
        return (
            _vector3(blade_explicit, "blade_direction_effector"),
            _vector3(slicing_explicit, "slicing_direction_effector"),
            _locked_joint_names(
                arguments.get("locked_joint_names", []),
                "locked_joint_names",
            ),
            (
                None
                if arguments.get("blade_profile_number") is None
                else _positive_profile_number(
                    arguments["blade_profile_number"],
                    "blade_profile_number",
                )
            ),
            "DEVELOPER_EXPLICIT",
        )
    if effector_profile is None:
        raise ValueError("the active mounted-effector profile is required")
    profiles = blade_profiles_from_effector(effector_profile)
    if not profiles["profiles"]:
        raise ValueError("the active mounted effector has no blade-use profiles")
    requested_number = arguments.get("blade_profile_number")
    selected = _profile_by_number(
        profiles,
        (
            profiles["default_profile_number"]
            if requested_number is None
            else requested_number
        ),
        kind="blade",
    )
    return (
        _vector3(selected["blade_direction_effector"], "blade_direction_effector"),
        _vector3(
            selected["slicing_direction_effector"],
            "slicing_direction_effector",
        ),
        _locked_joint_names(
            selected.get("locked_joint_names", []),
            "locked_joint_names",
        ),
        int(selected["profile_number"]),
        "ACTIVE_EFFECTOR_PROFILE",
    )


def _motion_policy(
    arguments: dict[str, Any],
    motion_profiles_document: dict[str, Any] | None,
) -> tuple[dict[str, Any], int | None, str]:
    fields = (
        "blade_load_kgf",
        "retract_distance_m",
        "delay_after_engage_s",
        "slice_wait_speed_m_s",
        "delay_after_retract_s",
    )
    explicit = [arguments.get(name) is not None for name in fields]
    if any(explicit):
        if not all(explicit):
            raise ValueError("all developer motion-profile values must be supplied")
        document = {
            "schema": MOTION_PROFILES_SCHEMA,
            "schema_version": 1,
            "default_profile_number": 1,
            "profiles": [
                {
                    "profile_number": 1,
                    "name": "developer-explicit",
                    **{name: arguments[name] for name in fields},
                }
            ],
        }
        selected = motion_profiles_from_document(document)["profiles"][0]
        profile_number = (
            None
            if arguments.get("motion_profile_number") is None
            else _positive_profile_number(
                arguments["motion_profile_number"],
                "motion_profile_number",
            )
        )
        return selected, profile_number, "DEVELOPER_EXPLICIT"
    if motion_profiles_document is None:
        raise ValueError("the Slicing motion-profile configuration is required")
    profiles = motion_profiles_from_document(motion_profiles_document)
    if not profiles["profiles"]:
        raise ValueError("the Slicing motion-profile configuration is empty")
    requested_number = arguments.get("motion_profile_number")
    selected = _profile_by_number(
        profiles,
        (
            profiles["default_profile_number"]
            if requested_number is None
            else requested_number
        ),
        kind="motion",
    )
    return selected, int(selected["profile_number"]), "SLICING_SKILL_CONFIG"


def select_active_workcell_activation(document: dict[str, Any]) -> dict[str, Any]:
    activations = document.get("activations")
    if not isinstance(activations, list):
        raise RuntimeError("Manager workcell calibration response is invalid")
    active = [
        item
        for item in activations
        if isinstance(item, dict)
        and item.get("state") == "ACTIVE"
        and item.get("motion_usable") is True
        and item.get("expires_at") is None
        and item.get("expires_at_us") is None
    ]
    if len(active) != 1:
        raise RuntimeError(
            "slicing requires exactly one active, motion-usable, non-expiring workcell calibration"
        )
    activation = copy.deepcopy(active[0])
    required_text = (
        "activation_id",
        "calibration_revision",
        "world_frame",
        "arm_base_frame",
    )
    if any(not str(activation.get(name) or "").strip() for name in required_text):
        raise RuntimeError("the active workcell calibration identity is incomplete")
    transforms = activation.get("transforms")
    world_from_base = transforms.get("world_from_base") if isinstance(transforms, dict) else None
    if not isinstance(world_from_base, dict):
        raise RuntimeError("the active calibration has no world_from_base transform")
    _vector3(world_from_base.get("translation_m", ()), "world_from_base.translation_m")
    _rotation_from_quaternion_xyzw(world_from_base.get("rotation_xyzw", ()))
    return activation


def activation_binding(activation: dict[str, Any]) -> dict[str, Any]:
    transform = activation["transforms"]["world_from_base"]
    return {
        "activation_id": str(activation["activation_id"]),
        "calibration_revision": str(activation["calibration_revision"]),
        "world_frame": str(activation["world_frame"]),
        "arm_base_frame": str(activation["arm_base_frame"]),
        "world_from_base": {
            "translation_m": [float(value) for value in transform["translation_m"]],
            "rotation_xyzw": [float(value) for value in transform["rotation_xyzw"]],
        },
    }


def same_activation_binding(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    if any(
        expected.get(name) != actual.get(name)
        for name in ("activation_id", "calibration_revision", "world_frame", "arm_base_frame")
    ):
        return False
    for name in ("translation_m", "rotation_xyzw"):
        if not np.allclose(
            expected["world_from_base"][name],
            actual["world_from_base"][name],
            atol=_TRANSFORM_TOLERANCE,
            rtol=0.0,
        ):
            return False
    return True


def resolve_point_arguments(
    arguments: dict[str, Any],
    activation: dict[str, Any],
    *,
    point_mode: str,
    current_effector_arm_base_m: Iterable[float] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve UI point entry into the absolute world points used by the Skill."""

    normalized_mode = str(point_mode or "").strip().upper()
    if normalized_mode not in {
        ABSOLUTE_WORLD_POINT_MODE,
        RELATIVE_WORLD_POINT_MODE,
    }:
        raise ValueError(
            "point_mode must be ABSOLUTE_WORLD or "
            "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
        )
    resolved = copy.deepcopy(arguments)
    entered_begin = _vector3(
        arguments.get(BEGIN_POINT_FIELD, ()),
        BEGIN_POINT_FIELD,
    )
    if normalized_mode == ABSOLUTE_WORLD_POINT_MODE:
        resolved[BEGIN_POINT_FIELD] = entered_begin.tolist()
        return resolved, {
            "point_mode": normalized_mode,
            "entered_values_semantics": (
                "ABSOLUTE_SLICE_BEGIN_POINT_IN_ACTIVE_WORLD_METERS"
            ),
            "captured_current_effector_world_m": None,
            "entered_slice_begin_point_m": entered_begin.tolist(),
            "resolved_slice_begin_point_world_m": entered_begin.tolist(),
        }

    if current_effector_arm_base_m is None:
        raise ValueError(
            "relative point mode requires a measured current effector position"
        )
    current_base = _vector3(
        current_effector_arm_base_m,
        "current_effector_arm_base_m",
    )
    binding = activation_binding(activation)
    transform = binding["world_from_base"]
    world_from_base_rotation = _rotation_from_quaternion_xyzw(
        transform["rotation_xyzw"]
    )
    world_from_base_translation = _vector3(
        transform["translation_m"],
        "world_from_base.translation_m",
    )
    current_world = (
        world_from_base_rotation @ current_base
        + world_from_base_translation
    )
    resolved_begin = current_world + entered_begin
    resolved[BEGIN_POINT_FIELD] = resolved_begin.tolist()
    return resolved, {
        "point_mode": normalized_mode,
        "entered_values_semantics": (
            "WORLD_AXIS_SLICE_BEGIN_OFFSET_FROM_CAPTURED_CURRENT_EFFECTOR_ORIGIN_METERS"
        ),
        "captured_current_effector_world_m": current_world.tolist(),
        "captured_current_effector_arm_base_m": current_base.tolist(),
        "entered_slice_begin_offset_world_m": entered_begin.tolist(),
        "resolved_slice_begin_point_world_m": resolved_begin.tolist(),
    }


@dataclass(frozen=True)
class SlicingPlan:
    integrated_alignment_arguments: dict[str, Any]
    contact_steps: tuple[ContactStep, ContactStep, ContactStep]
    workcell_binding: dict[str, Any]
    blade_profile_number: int | None
    motion_profile_number: int | None
    alignment: dict[str, Any]
    path: dict[str, Any]
    timing: dict[str, Any]
    load: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": SKILL_ID,
            "integrated_alignment_arguments": copy.deepcopy(
                self.integrated_alignment_arguments
            ),
            "contact_steps": [
                {
                    "phase": phase,
                    "motion_type": step.motion_type,
                    "position_mode": step.position_mode,
                    "position_m": list(step.position_m),
                    "orientation_xyzw": list(step.orientation_xyzw),
                    "force_n": list(step.force_n),
                    "torque_nm": list(step.torque_nm),
                    "locked_joint_names": list(step.locked_joint_names),
                    "delay_after_accept_s": step.delay_after_accept_s,
                    "next_command_timeout_s": step.next_command_timeout_s,
                    "wrench_frame": "ARM_ROOT",
                }
                for phase, step in zip(
                    ("ENGAGE", "SLICE", "RETRACT"), self.contact_steps
                )
            ],
            "workcell_binding": copy.deepcopy(self.workcell_binding),
            "blade_profile_number": self.blade_profile_number,
            "motion_profile_number": self.motion_profile_number,
            "alignment": copy.deepcopy(self.alignment),
            "path": copy.deepcopy(self.path),
            "timing": copy.deepcopy(self.timing),
            "load": copy.deepcopy(self.load),
            "task_success_assessed": False,
        }


def build_slicing_plan(
    arguments: dict[str, Any],
    activation: dict[str, Any],
    *,
    effector_profile: dict[str, Any] | None = None,
    motion_profiles_document: dict[str, Any] | None = None,
) -> SlicingPlan:
    integrated_execution_backend = str(
        arguments.get("integrated_execution_backend") or "IMPEDANCE"
    ).strip().upper()
    if integrated_execution_backend not in {"IMPEDANCE", "POS_SPEED"}:
        raise ValueError(
            "integrated_execution_backend must be IMPEDANCE or POS_SPEED"
        )
    (
        blade_effector_raw,
        slicing_effector_raw,
        locked_joint_names,
        blade_profile_number,
        blade_profile_source,
    ) = _blade_use(
        arguments,
        effector_profile,
    )
    (
        motion_policy,
        motion_profile_number,
        motion_profile_source,
    ) = _motion_policy(
        arguments,
        motion_profiles_document,
    )
    blade_effector = _unit(blade_effector_raw, "blade_direction_effector")
    slicing_effector = _projected_unit(
        slicing_effector_raw,
        blade_effector,
        "slicing_direction_effector",
    )
    third_effector = _unit(
        np.cross(blade_effector, slicing_effector),
        "effector third direction",
    )

    blade_world = _unit(
        _vector3(
            arguments["blade_direction_world"],
            "blade_direction_world",
        ),
        "blade_direction_world",
    )
    engage_world = _vector3(
        arguments[BEGIN_POINT_FIELD],
        BEGIN_POINT_FIELD,
    )
    slicing_world = _projected_unit(
        _vector3(
            arguments["slicing_direction_world"],
            "slicing_direction_world",
        ),
        blade_world,
        "slicing_direction_world",
    )
    try:
        slice_length_m = float(arguments["slice_length_m"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("slice_length_m must be a positive finite number") from exc
    if not math.isfinite(slice_length_m) or slice_length_m <= 0.0:
        raise ValueError("slice_length_m must be a positive finite number")
    retract_distance_m = float(motion_policy["retract_distance_m"])
    slice_world = engage_world + slicing_world * slice_length_m
    retract_world = slice_world - blade_world * retract_distance_m
    third_world = _unit(
        np.cross(blade_world, slicing_world),
        "world third direction",
    )

    effector_basis = np.column_stack((blade_effector, slicing_effector, third_effector))
    world_basis = np.column_stack((blade_world, slicing_world, third_world))
    world_from_effector_rotation = world_basis @ effector_basis.T
    _rotation_quality(world_from_effector_rotation)

    binding = activation_binding(activation)
    world_from_base = binding["world_from_base"]
    world_from_base_rotation = _rotation_from_quaternion_xyzw(
        world_from_base["rotation_xyzw"]
    )
    world_from_base_translation = _vector3(
        world_from_base["translation_m"], "world_from_base.translation_m"
    )
    base_from_world_rotation = world_from_base_rotation.T
    base_from_effector_rotation = (
        base_from_world_rotation @ world_from_effector_rotation
    )
    _rotation_quality(base_from_effector_rotation)
    orientation_xyzw = _quaternion_xyzw_from_rotation(base_from_effector_rotation)
    target_rpy = _rpy_from_rotation(base_from_effector_rotation)

    blade_load_kgf = float(motion_policy["blade_load_kgf"])
    load_n = blade_load_kgf * KGF_TO_NEWTON
    force_world = load_n * (
        blade_world + 0.5 * slicing_world + 0.5 * third_world
    )
    force_base = base_from_world_rotation @ force_world

    engage_delay = float(motion_policy["delay_after_engage_s"])
    slice_wait_speed = float(motion_policy["slice_wait_speed_m_s"])
    slice_delay = slice_length_m / slice_wait_speed
    retract_delay = float(motion_policy["delay_after_retract_s"])
    if not math.isfinite(slice_delay) or not 0.0 <= slice_delay <= 55.0:
        raise ValueError(
            "slice_length_m / slice_wait_speed_m_s must be in [0, 55] seconds"
        )

    planned_positions_base = tuple(
        base_from_world_rotation @ (point - world_from_base_translation)
        for point in (engage_world, slice_world, retract_world)
    )
    slice_delta_base = base_from_world_rotation @ (
        slicing_world * slice_length_m
    )
    retract_delta_base = base_from_world_rotation @ (
        -blade_world * retract_distance_m
    )
    contact_positions_base = (
        planned_positions_base[0],
        slice_delta_base,
        retract_delta_base,
    )
    position_modes = (
        "ABSOLUTE_ROOT",
        "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
        "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
    )
    delays = (engage_delay, slice_delay, retract_delay)
    steps = tuple(
        ContactStep(
            position_m=tuple(float(value) for value in position),
            orientation_xyzw=orientation_xyzw,
            force_n=tuple(float(value) for value in force_base),
            torque_nm=(0.0, 0.0, 0.0),
            motion_type="CARTESIAN_SEGMENT",
            position_mode=position_mode,
            locked_joint_names=locked_joint_names,
            delay_after_accept_s=delay,
            next_command_timeout_s=max(6.0, delay + 1.0),
            wrench_in_acting_frame=False,
        )
        for position, position_mode, delay in zip(
            contact_positions_base,
            position_modes,
            delays,
        )
    )
    if len(steps) != 3:
        raise AssertionError("slicing must create exactly three Contact steps")

    return SlicingPlan(
        integrated_alignment_arguments={
            "direction": "NONE",
            "distance_m": 0.0,
            "reference_frame": "ARM_BASE",
            "arm_mount_assumption": "UNKNOWN",
            "camera_level_assumption": "UNKNOWN",
            "fixed_vio_rig_assumption": "UNKNOWN",
            "orientation_policy": "SET_ARM_BASE_RPY",
            "target_orientation_rpy_rad": list(target_rpy),
            "execution_backend": integrated_execution_backend,
        },
        contact_steps=steps,  # type: ignore[arg-type]
        workcell_binding=binding,
        blade_profile_number=blade_profile_number,
        motion_profile_number=motion_profile_number,
        alignment={
            "blade_profile_source": blade_profile_source,
            "blade_profile_selection": (
                "DEVELOPER_EXPLICIT_VALUES"
                if blade_profile_source == "DEVELOPER_EXPLICIT"
                else (
                    "LIVE_DEFAULT"
                    if arguments.get("blade_profile_number") is None
                    else "EXPLICIT_NUMBER"
                )
            ),
            "blade_profile_number": blade_profile_number,
            "locked_joint_names": list(locked_joint_names),
            "integrated_execution_backend": integrated_execution_backend,
            "blade_direction_effector": blade_effector.tolist(),
            "projected_slicing_direction_effector": slicing_effector.tolist(),
            "blade_direction_world": blade_world.tolist(),
            "derived_projected_slicing_direction_world": slicing_world.tolist(),
            "derived_third_direction_world": third_world.tolist(),
            "target_orientation_arm_base_xyzw": list(orientation_xyzw),
            "target_orientation_arm_base_rpy_rad": list(target_rpy),
            "priority": "BLADE_EXACT_THEN_SLICING_PROJECTION",
        },
        path={
            "slice_begin_point_world_m": engage_world.tolist(),
            "projected_slicing_direction_world": slicing_world.tolist(),
            "requested_slice_length_m": slice_length_m,
            "slice_endpoint_world_m": slice_world.tolist(),
            "slice_position_mode": (
                "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES"
            ),
            "slice_delta_arm_base_m": slice_delta_base.tolist(),
            "retract_direction_world": (-blade_world).tolist(),
            "retract_distance_m": retract_distance_m,
            "planned_retract_endpoint_world_m": retract_world.tolist(),
            "retract_position_mode": (
                "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES"
            ),
            "retract_delta_arm_base_m": retract_delta_base.tolist(),
            "construction": (
                "ABSOLUTE_BEGIN_THEN_MEASURED_START_PROJECTED_SLICE_"
                "THEN_MEASURED_START_NEGATIVE_BLADE_RETRACT"
            ),
        },
        timing={
            "motion_profile_source": motion_profile_source,
            "motion_profile_selection": (
                "DEVELOPER_EXPLICIT_VALUES"
                if motion_profile_source == "DEVELOPER_EXPLICIT"
                else (
                    "LIVE_DEFAULT"
                    if arguments.get("motion_profile_number") is None
                    else "EXPLICIT_NUMBER"
                )
            ),
            "motion_profile_number": motion_profile_number,
            "delay_after_engage_s": engage_delay,
            "slice_wait_speed_m_s": slice_wait_speed,
            "computed_delay_after_slice_s": slice_delay,
            "delay_after_retract_s": retract_delay,
            "slice_wait_semantics": (
                "PLANNED_COMMAND_SPACING_FROM_LENGTH_DIVIDED_BY_RATE;"
                "NOT_CARTESIAN_SPEED_CONTROL"
            ),
            "velocity_limited_hold_policy": (
                "RUNTIME_MAX_OF_PROFILE_DELAY_AND_PROVIDER_DERIVED_"
                "JOINT_TRANSITION_FLOOR"
            ),
        },
        load={
            "motion_profile_source": motion_profile_source,
            "motion_profile_number": motion_profile_number,
            "blade_load_kgf": blade_load_kgf,
            "kgf_to_newton": KGF_TO_NEWTON,
            "components_world_basis_kgf": [
                blade_load_kgf,
                0.5 * blade_load_kgf,
                0.5 * blade_load_kgf,
            ],
            "force_world_n": force_world.tolist(),
            "force_arm_base_n": force_base.tolist(),
            "torque_nm": [0.0, 0.0, 0.0],
        },
    )
