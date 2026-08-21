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
from grip_work_runtime.point_entry import finite_vector3, normalize_point_mode
from lay_flat_skill.development_execution import LayFlatDevelopmentExecution


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
        "name": normalized_profile_name(payload.get("name"), "Lay-flat motion"),
        "table_inward_distance_m": finite_number(
            payload.get("table_inward_distance_m"),
            "table_inward_distance_m",
            minimum=0.0,
            maximum=0.25,
            exclusive_minimum=True,
        ),
        "negative_insertion_distance_m": finite_number(
            payload.get("negative_insertion_distance_m"),
            "negative_insertion_distance_m",
            minimum=0.0,
            maximum=0.25,
            exclusive_minimum=True,
        ),
        "retreat_distance_m": finite_number(
            payload.get("retreat_distance_m"),
            "retreat_distance_m",
            minimum=0.0,
            maximum=0.25,
            exclusive_minimum=True,
        ),
        "mit_delta_time_s": finite_number(
            payload.get("mit_delta_time_s"),
            "mit_delta_time_s",
            minimum=0.02,
            maximum=5.0,
        ),
        "open_timeout_s": finite_number(
            payload.get("open_timeout_s"),
            "open_timeout_s",
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
    resolved_motion = {}
    for key in (
        "table_inward_distance_m",
        "negative_insertion_distance_m",
        "retreat_distance_m",
        "mit_delta_time_s",
        "open_timeout_s",
    ):
        value = payload.get(key)
        resolved_motion[key] = motion_profile[key] if value is None else value
    resolved_motion = _motion_profile(
        {"name": motion_profile["name"], **resolved_motion}
    )
    return {
        "schema": "midbrain.skill.lay_flat.development_plan",
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
        "approach_begin_point_world_m": finite_vector3(
            payload.get("approach_begin_point_world_m"),
            "approach_begin_point_world_m",
        ),
        "motion": resolved_motion,
        "stages": [
            "INTEGRATED_ROTATION_ONLY",
            "CONTACT_ABSOLUTE_APPROACH_PLUS_TABLE_INWARD_PLUS_NEGATIVE_INSERTION",
            "GRIPPER_RELEASE_AND_MIT_FLOAT",
            "CONTACT_NEGATIVE_INSERTION_RETREAT_AND_RELAX",
        ],
        "execution": "SKILL_OWNED_ATTENDED_INTEGRATED_THEN_CONTACT_DEVELOPMENT",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", required=True)
    parser.add_argument("--port", type=int, default=7116)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--contact-url", default="http://127.0.0.1:8794")
    parser.add_argument("--grip-url", default="http://127.0.0.1:8795")
    parser.add_argument("--integrated-url", default="http://127.0.0.1:8793")
    parser.add_argument("--authorization-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    root = Path(args.skill_root).resolve()
    vector_store = NumberedProfileStore(
        root / "config/gripper_vector_profiles.json",
        expected_schema="midbrain.lay_flat_gripper_vector_profiles",
        validator=_vector_profile,
    )
    motion_store = NumberedProfileStore(
        root / "config/motion_profiles.json",
        expected_schema="midbrain.lay_flat_motion_profiles",
        validator=_motion_profile,
    )
    staged_execution = LayFlatDevelopmentExecution(
        skill_root=root,
        manager_url=args.manager_url,
        contact_url=args.contact_url,
        grip_url=args.grip_url,
        integrated_url=args.integrated_url,
        authorization_url=args.authorization_url,
    )
    run_development_server(
        port=args.port,
        title="Action: lay gripped object flat Developer",
        skill_kind="lay_flat",
        vector_store=vector_store,
        motion_store=motion_store,
        motion_fields=[
            {"key": "table_inward_distance_m", "label": "Table-inward distance (m)", "minimum": 0.001, "maximum": 0.25},
            {"key": "negative_insertion_distance_m", "label": "Negative insertion distance (m)", "minimum": 0.001, "maximum": 0.25},
            {"key": "retreat_distance_m", "label": "Retreat distance (m)", "minimum": 0.001, "maximum": 0.25},
            {"key": "mit_delta_time_s", "label": "MIT float transition (s)", "minimum": 0.02, "maximum": 5.0},
            {"key": "open_timeout_s", "label": "Measured-open timeout (s)", "minimum": 0.1, "maximum": 30.0},
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
