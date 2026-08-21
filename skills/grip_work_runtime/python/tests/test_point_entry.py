from __future__ import annotations

import math

import pytest

from grip_work_runtime.point_entry import (
    ABSOLUTE_WORLD_POINT_MODE,
    RELATIVE_WORLD_POINT_MODE,
    resolve_approach_point,
)


def world_from_base() -> dict:
    half = math.sqrt(0.5)
    return {
        "translation_m": [10.0, 20.0, 30.0],
        "rotation_xyzw": [0.0, 0.0, half, half],
    }


def test_relative_approach_point_freezes_one_current_effector_origin() -> None:
    resolved = resolve_approach_point(
        [0.0, 1.0, 0.0],
        point_mode=RELATIVE_WORLD_POINT_MODE,
        world_from_base=world_from_base(),
        current_effector_base_m=[1.0, 0.0, 0.0],
    )

    assert resolved["captured_current_effector_world_m"] == pytest.approx(
        [10.0, 21.0, 30.0]
    )
    assert resolved["resolved_approach_begin_point_world_m"] == pytest.approx(
        [10.0, 22.0, 30.0]
    )
    assert resolved["resolved_approach_begin_point_base_m"] == pytest.approx(
        [2.0, 0.0, 0.0]
    )
    assert resolved["origin_capture_policy"] == "CAPTURED_DURING_PREPARATION"


def test_absolute_approach_point_does_not_capture_current_effector() -> None:
    resolved = resolve_approach_point(
        [10.0, 22.0, 30.0],
        point_mode=ABSOLUTE_WORLD_POINT_MODE,
        world_from_base=world_from_base(),
    )

    assert resolved["captured_current_effector_world_m"] is None
    assert resolved["resolved_approach_begin_point_base_m"] == pytest.approx(
        [2.0, 0.0, 0.0]
    )
