from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import math

from grip_work_runtime.development import (
    NumberedProfileStore,
    finite_number,
    normalized_profile_name,
    vector3,
)
from grip_work_runtime.development_server import run_development_server
from grip_work_runtime.point_entry import normalize_point_mode
from grip_object_skill.development_execution import ScrapGripDevelopmentExecution


def _qualified_pair(first: Any, second: Any, prefix: str) -> tuple[list[float], list[float]]:
    first_value = vector3(first, f"{prefix} table-inward direction")
    second_value = vector3(second, f"{prefix} insertion direction")
    first_norm = math.sqrt(sum(value * value for value in first_value))
    unit_first = [value / first_norm for value in first_value]
    projection = sum(a * b for a, b in zip(unit_first, second_value))
    residual = [
        value - projection * unit_first[index]
        for index, value in enumerate(second_value)
    ]
    if math.sqrt(sum(value * value for value in residual)) < 1e-6:
        raise ValueError(f"{prefix} directions must not be parallel")
    return first_value, second_value


def _point3(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    return [finite_number(component, name) for component in value]


def _vector_profile(payload: dict[str, Any]) -> dict[str, Any]:
    inward, insertion = _qualified_pair(
        payload.get("table_inward_direction_effector"),
        payload.get("insertion_direction_effector"),
        "gripper",
    )
    return {
        "name": normalized_profile_name(payload.get("name"), "Gripper vectors"),
        "table_inward_direction_effector": inward,
        "insertion_direction_effector": insertion,
        "qualification": "DEVELOPMENT_REQUIRES_ATTENDED_PHYSICAL_TUNING",
    }


def _motion_profile(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": normalized_profile_name(payload.get("name"), "Scrap grip motion"),
        "table_inward_distance_m": finite_number(
            payload.get("table_inward_distance_m"),
            "table_inward_distance_m",
            minimum=0.0,
            maximum=0.25,
            exclusive_minimum=True,
        ),
        "insertion_distance_m": finite_number(
            payload.get("insertion_distance_m"),
            "insertion_distance_m",
            minimum=0.0,
            maximum=0.25,
            exclusive_minimum=True,
        ),
        "delay_after_lower_s": finite_number(
            payload.get("delay_after_lower_s"),
            "delay_after_lower_s",
            minimum=0.0,
            maximum=55.0,
        ),
        "delay_after_scrap_s": finite_number(
            payload.get("delay_after_scrap_s"),
            "delay_after_scrap_s",
            minimum=0.0,
            maximum=55.0,
        ),
        "delay_after_grip_s": finite_number(
            payload.get("delay_after_grip_s"),
            "delay_after_grip_s",
            minimum=0.0,
            maximum=55.0,
        ),
        "grip_position_rad": finite_number(
            payload.get("grip_position_rad"), "grip_position_rad"
        ),
        "grip_velocity_rad_s": finite_number(
            payload.get("grip_velocity_rad_s"),
            "grip_velocity_rad_s",
            minimum=0.0,
            exclusive_minimum=True,
        ),
        "gripping_torque_limit_nm": finite_number(
            payload.get("gripping_torque_limit_nm"),
            "gripping_torque_limit_nm",
            minimum=0.0,
            maximum=1.4,
            exclusive_minimum=True,
        ),
        "contact_timeout_s": finite_number(
            payload.get("contact_timeout_s"),
            "contact_timeout_s",
            minimum=0.1,
            maximum=30.0,
        ),
        "qualification": "DEVELOPMENT_REQUIRES_ATTENDED_PHYSICAL_TUNING",
    }


def _selected(store: NumberedProfileStore, requested: Any) -> dict[str, Any]:
    document = store.snapshot()
    number = (
        int(document["default_profile_number"])
        if requested is None
        else int(requested)
    )
    profile = next(
        (
            value
            for value in document["profiles"]
            if int(value["profile_number"]) == number
        ),
        None,
    )
    if not isinstance(profile, dict):
        raise ValueError(f"profile #{number} is unavailable")
    return profile


def _prepare(
    payload: dict[str, Any],
    vector_store: NumberedProfileStore,
    motion_store: NumberedProfileStore,
) -> dict[str, Any]:
    vector_profile = _selected(
        vector_store, payload.get("gripper_vector_profile_number")
    )
    motion_profile = _selected(motion_store, payload.get("motion_profile_number"))
    effector_inward, effector_insertion = _qualified_pair(
        payload.get("table_inward_direction_effector"),
        payload.get("insertion_direction_effector"),
        "gripper",
    )
    world_inward, world_insertion = _qualified_pair(
        payload.get("table_inward_direction_world"),
        payload.get("insertion_direction_world"),
        "object",
    )
    binding = payload.get("object_binding")
    if not isinstance(binding, dict) or not binding:
        raise ValueError("object_binding must be a non-empty object")
    resolved_motion = {}
    for key in (
        "table_inward_distance_m",
        "insertion_distance_m",
        "delay_after_lower_s",
        "delay_after_scrap_s",
        "delay_after_grip_s",
        "grip_position_rad",
        "grip_velocity_rad_s",
        "gripping_torque_limit_nm",
        "contact_timeout_s",
    ):
        value = payload.get(key)
        resolved_motion[key] = motion_profile[key] if value is None else value
    resolved_motion = _motion_profile(
        {"name": motion_profile["name"], **resolved_motion}
    )
    return {
        "schema": "midbrain.skill.scrap_grip.development_plan",
        "schema_version": 1,
        "physical_motion_requested": False,
        "gripper_vector_profile_number": vector_profile["profile_number"],
        "motion_profile_number": motion_profile["profile_number"],
        "typed_vectors_match_selected_profile": (
            effector_inward == vector_profile["table_inward_direction_effector"]
            and effector_insertion == vector_profile["insertion_direction_effector"]
        ),
        "gripper_vectors": {
            "table_inward_direction_effector": effector_inward,
            "insertion_direction_effector": effector_insertion,
        },
        "object_vectors": {
            "table_inward_direction_world": world_inward,
            "insertion_direction_world": world_insertion,
        },
        "point_mode": normalize_point_mode(payload.get("point_mode")),
        "approach_begin_point_world_m": _point3(
            payload.get("approach_begin_point_world_m"),
            "approach_begin_point_world_m",
        ),
        "motion": resolved_motion,
        "object_binding": binding,
        "stages": [
            "INTEGRATED_ROTATION_ONLY_WITH_CONCURRENT_GRIPPER_OPENING",
            "CONTACT_ABSOLUTE_APPROACH_PLUS_TABLE_INWARD",
            "CONTACT_INSERTION",
            "GRIP_AND_CONFIRM_CARRY",
        ],
        "execution": "SKILL_OWNED_ATTENDED_INTEGRATED_THEN_CONTACT_DEVELOPMENT",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", required=True)
    parser.add_argument("--port", type=int, default=7115)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--contact-url", default="http://127.0.0.1:8794")
    parser.add_argument("--grip-url", default="http://127.0.0.1:8795")
    parser.add_argument("--integrated-url", default="http://127.0.0.1:8793")
    parser.add_argument("--authorization-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    root = Path(args.skill_root).resolve()
    vector_store = NumberedProfileStore(
        root / "config/gripper_vector_profiles.json",
        expected_schema="midbrain.scrap_grip_gripper_vector_profiles",
        validator=_vector_profile,
    )
    motion_store = NumberedProfileStore(
        root / "config/motion_profiles.json",
        expected_schema="midbrain.grip_motion_profiles",
        validator=_motion_profile,
    )
    staged_execution = ScrapGripDevelopmentExecution(
        skill_root=root,
        manager_url=args.manager_url,
        contact_url=args.contact_url,
        grip_url=args.grip_url,
        integrated_url=args.integrated_url,
        authorization_url=args.authorization_url,
    )
    run_development_server(
        port=args.port,
        title="Action: scrap grip Developer",
        skill_kind="scrap_grip",
        vector_store=vector_store,
        motion_store=motion_store,
        motion_fields=[
            {"key": "table_inward_distance_m", "label": "Table-inward distance (m)", "minimum": 0.001, "maximum": 0.25},
            {"key": "insertion_distance_m", "label": "Insertion distance (m)", "minimum": 0.001, "maximum": 0.25},
            {"key": "delay_after_lower_s", "label": "Wait after lower (s)", "minimum": 0.0, "maximum": 55.0},
            {"key": "delay_after_scrap_s", "label": "Wait after scrap (s)", "minimum": 0.0, "maximum": 55.0},
            {"key": "delay_after_grip_s", "label": "Wait after grip command (s)", "minimum": 0.0, "maximum": 55.0},
            {"key": "grip_position_rad", "label": "Grip target position (rad)"},
            {"key": "grip_velocity_rad_s", "label": "Grip velocity limit (rad/s)", "minimum": 0.001},
            {"key": "gripping_torque_limit_nm", "label": "Grip motor torque ceiling (Nm)", "minimum": 0.001, "maximum": 1.4},
            {"key": "contact_timeout_s", "label": "Contact inference timeout (s)", "minimum": 0.1, "maximum": 30.0},
        ],
        default_inputs={
            "approach_begin_point_world_m": [0.0, 0.0, 0.0],
            "table_inward_direction_world": [0.0, 0.0, -1.0],
            "insertion_direction_world": [1.0, 0.0, 0.0],
        },
        prepare=lambda payload: _prepare(payload, vector_store, motion_store),
        grip_state_url=f"{args.grip_url.rstrip('/')}/v1/grip/state",
        staged_execution=staged_execution,
    )


if __name__ == "__main__":
    main()
