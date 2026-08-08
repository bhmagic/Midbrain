from __future__ import annotations

import copy

import numpy as np
import pytest

from refine_arm_root_translation import (
    apply_compact_translation_update,
    build_quality_review_prompt,
    finalize_translation_refinement,
    prepare_translation_refinement,
    validate_quality_review,
)


IDENTITIES = {
    "world_frame": "local_vio/epoch-1",
    "vio_session_epoch": "epoch-1",
    "spatial_convention": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
    "camera_provider_id": "camera.orbbec",
    "camera_boot_id": "camera-boot",
    "camera_calibration_revision": "rgbd-cal-1",
    "arm_provider_id": "robot.rebot",
    "arm_boot_id": "arm-boot",
    "arm_model_id": "rebot_arm_b601_dm",
    "arm_model_revision": "rebot-official-fixed-end-0.1.21-pos-speed-motor-envelope",
    "effector_profile_revision": "rebot-b601-dm-gripper-alignment-v3",
}


def rz90() -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = np.asarray(
        [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
    )
    result[:3, 3] = [1.0, 2.0, 3.0]
    return result


def proposal(
    *,
    adoption_factor: float = 1.0,
    delta: np.ndarray | None = None,
    threshold: float = 0.005,
    revision: int = 7,
    active: np.ndarray | None = None,
) -> dict:
    world_from_base = rz90() if active is None else active
    base_from_tool = np.eye(4, dtype=np.float64)
    base_from_tool[:3, 3] = [0.1, 0.0, 0.0]
    tool_point = np.asarray([0.2, 0.0, 0.0])
    base_point = base_from_tool[:3, :3] @ tool_point + base_from_tool[:3, 3]
    raw_delta = np.asarray(
        [0.01, -0.02, 0.03] if delta is None else delta,
        dtype=np.float64,
    )
    observed_world = (
        world_from_base[:3, :3] @ base_point
        + world_from_base[:3, 3]
        + raw_delta
    )
    return prepare_translation_refinement(
        active_world_from_base=world_from_base,
        base_from_tool=base_from_tool,
        tool_landmark_point_m=tool_point,
        observed_world_landmark_point_m=observed_world,
        adoption_factor=adoption_factor,
        review_threshold_m=threshold,
        source_revision=revision,
        identities=IDENTITIES,
        landmark_id="rail_lateral_endpoint_mean",
        observation_provenance={"frame_number": 10, "observed_at_us": 123},
    )


def compact_state(*, revision: int = 7, transform: np.ndarray | None = None) -> dict:
    return {
        "schema": "midbrain.compact_arm_root_alignment_state",
        "schema_version": 1,
        "revision": revision,
        "world_from_base": (rz90() if transform is None else transform).tolist(),
        "identities": copy.deepcopy(IDENTITIES),
        "last_update": None,
    }


def passed_review() -> dict:
    return {
        "schema": "midbrain.effector_landmark_quality_review",
        "schema_version": 1,
        "landmark_id": "rail_lateral_endpoint_mean",
        "verdict": "PASS",
        "reason": "Both endpoint and depth marks match the neon-green rail.",
        "reviewed_point_ids": ["rail_lateral_left", "rail_lateral_right"],
    }


def test_translation_only_math_preserves_rotation_exactly() -> None:
    result = proposal(adoption_factor=0.25, threshold=1.0)
    source = np.asarray(result["source_world_from_base"])
    proposed = np.asarray(result["proposed_world_from_base"])

    assert np.array_equal(source[:3, :3], proposed[:3, :3])
    assert result["rotation_change_rad"] == 0.0
    assert np.allclose(result["raw_translation_delta_m"], [0.01, -0.02, 0.03])
    assert np.allclose(result["adopted_translation_delta_m"], [0.0025, -0.005, 0.0075])
    assert np.allclose(proposed[:3, 3], [1.0025, 1.995, 3.0075])


@pytest.mark.parametrize(
    ("factor", "expected"),
    [(0.0, [0.0, 0.0, 0.0]), (0.5, [0.005, -0.01, 0.015]), (1.0, [0.01, -0.02, 0.03])],
)
def test_caller_controls_adoption_factor(factor: float, expected: list[float]) -> None:
    result = proposal(adoption_factor=factor, threshold=1.0)

    assert np.allclose(result["adopted_translation_delta_m"], expected)


def test_zero_adoption_is_observation_only_but_reviews_large_measurement() -> None:
    result = proposal(adoption_factor=0.0, delta=np.asarray([1.0, 0.0, 0.0]))

    assert result["status"] == "OBSERVATION_ONLY"
    assert result["workflow_complete"]
    assert result["quality_review"]["required"]
    assert not result["eligible_for_state_update"]


def test_passed_zero_adoption_review_remains_observation_only() -> None:
    pending = proposal(
        adoption_factor=0.0,
        delta=np.asarray([1.0, 0.0, 0.0]),
    )
    review = validate_quality_review(
        passed_review(),
        landmark={
            "landmark_id": "rail_lateral_endpoint_mean",
            "required_point_ids": ["rail_lateral_left", "rail_lateral_right"],
        },
    )

    result = finalize_translation_refinement(pending, quality_review=review)

    assert result["status"] == "OBSERVATION_ONLY"
    assert result["quality_review"]["verdict"] == "PASS"
    assert not result["eligible_for_state_update"]


def test_second_review_threshold_uses_raw_delta_before_adoption() -> None:
    result = proposal(
        adoption_factor=0.1,
        delta=np.asarray([0.01, 0.0, 0.0]),
        threshold=0.005,
    )

    assert np.isclose(result["raw_translation_delta_norm_m"], 0.01)
    assert np.allclose(result["adopted_translation_delta_m"], [0.001, 0.0, 0.0])
    assert result["quality_review"]["required"]
    assert result["status"] == "SECOND_VLM_REVIEW_REQUIRED"


def test_passed_second_review_enables_update() -> None:
    pending = proposal()
    review = validate_quality_review(
        passed_review(),
        landmark={
            "landmark_id": "rail_lateral_endpoint_mean",
            "required_point_ids": ["rail_lateral_left", "rail_lateral_right"],
        },
    )

    result = finalize_translation_refinement(pending, quality_review=review)

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["eligible_for_state_update"]
    assert result["quality_review"]["verdict"] == "PASS"


@pytest.mark.parametrize("verdict", ["FAIL", "UNRESOLVED"])
def test_failed_or_unresolved_second_review_rejects_update(verdict: str) -> None:
    review = passed_review()
    review["verdict"] = verdict

    result = finalize_translation_refinement(proposal(), quality_review=review)

    assert result["status"] == "REJECTED_QUALITY_REVIEW"
    assert not result["eligible_for_state_update"]


def test_compact_state_update_has_no_parent_chain_or_history() -> None:
    state = compact_state()
    ready = finalize_translation_refinement(
        proposal(adoption_factor=0.5),
        quality_review=passed_review(),
    )

    updated = apply_compact_translation_update(state, ready)

    assert updated["revision"] == 8
    assert set(updated) == {
        "schema",
        "schema_version",
        "revision",
        "world_from_base",
        "identities",
        "last_update",
    }
    assert "parent" not in json_text(updated)
    assert "history" not in json_text(updated)


def json_text(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def test_many_calls_keep_one_constant_size_active_state() -> None:
    state = compact_state(revision=0)
    initial_keys = set(state)
    for _ in range(100):
        active = np.asarray(state["world_from_base"], dtype=np.float64)
        ready = proposal(
            adoption_factor=0.5,
            delta=np.asarray([0.001, 0.0, 0.0]),
            threshold=1.0,
            revision=state["revision"],
            active=active,
        )
        state = apply_compact_translation_update(state, ready)

    assert state["revision"] == 100
    assert set(state) == initial_keys
    assert len(json_text(state)) < 2000


def test_observation_only_does_not_increment_revision() -> None:
    state = compact_state()
    observed = proposal(adoption_factor=0.0, threshold=1.0)

    updated = apply_compact_translation_update(state, observed)

    assert updated == state


def test_stale_revision_is_rejected_atomically() -> None:
    state = compact_state(revision=8)
    stale = proposal(adoption_factor=1.0, threshold=1.0, revision=7)

    with pytest.raises(RuntimeError, match="stale"):
        apply_compact_translation_update(state, stale)


def test_quality_review_prompt_forbids_coordinate_correction() -> None:
    prompt = build_quality_review_prompt(
        profile={"display_name": "Test Gripper"},
        landmark={
            "landmark_id": "rail_lateral_endpoint_mean",
            "required_point_ids": ["rail_lateral_left", "rail_lateral_right"],
        },
        raw_translation_delta_m=[0.01, 0.0, 0.0],
        raw_translation_delta_norm_m=0.01,
    )

    assert "Do not propose replacement coordinates" in prompt
    assert "PASS, FAIL, or UNRESOLVED" in prompt
    assert "exactly these six keys and no others" in prompt
    assert "midbrain.effector_landmark_quality_review" in prompt
    assert "landmark_id exactly to rail_lateral_endpoint_mean" in prompt
    assert "rail_lateral_left, rail_lateral_right" in prompt
    assert "Do not wrap the JSON in Markdown" in prompt


def test_quality_review_schema_mismatch_is_actionable() -> None:
    malformed = passed_review()
    malformed.pop("schema_version")
    malformed["extra"] = True

    with pytest.raises(RuntimeError) as captured:
        validate_quality_review(
            malformed,
            landmark={
                "landmark_id": "rail_lateral_endpoint_mean",
                "required_point_ids": [
                    "rail_lateral_left",
                    "rail_lateral_right",
                ],
            },
        )

    message = str(captured.value)
    assert "missing: schema_version" in message
    assert "unexpected: extra" in message


def test_result_never_contains_motion_authority() -> None:
    result = proposal(adoption_factor=1.0, threshold=1.0)

    assert result["physical_motion_submitted"] is False
    assert result["physical_motion_authorized"] is False
    assert "target_pose" not in result
    assert "controller_preview" not in result
