from __future__ import annotations

import copy

import pytest

from vegetable_cutting.config import load_skill_config, validate_config


def test_default_config_requires_reviewed_supervised_motion() -> None:
    config = load_skill_config()
    assert config["handoff"]["dry_run_required"] is True
    assert config["handoff"]["operator_takeover_required"] is True
    assert config["handoff"]["allow_below_board_target"] is False
    assert config["tracking"]["vlm_after_each_cut"] is False
    assert config["execution"]["enabled"] is True
    assert config["execution"]["require_preview_every_commit"] is True
    assert config["execution"]["first_cut_maximum_vlm_attempts"] == 5
    assert config["handoff"]["mit_kp_multiplier"] == 10.0
    assert config["execution"]["transfer_kp_multiplier"] == 1.5
    assert config["execution"]["retract_kp_multiplier"] == 10.0
    assert config["execution"]["post_cut_retract_m"] == 0.1
    assert (
        config["execution"][
            "first_cut_maximum_translation_per_iteration_mm"
        ]
        == 50.0
    )
    assert (
        config["execution"][
            "first_cut_minimum_consecutive_direction_cosine"
        ]
        == -0.25
    )
    assert config["execution"]["maximum_translation_per_commit_m"] <= 0.1
    assert config["handoff"]["motion_backend"] == "PRESS_MIT"
    assert config["tool"]["registration_mode"] == "FIXED_HARD_MOUNT"
    assert config["tool"]["vlm_blade_registration_optional"] is True
    assert config["tool"]["fixed_controlled_frame_offset_xyz_m"] == [
        0.18,
        0.0,
        -0.02,
    ]
    assert config["tool"]["fixed_controlled_frame_offset_rpy_rad"] == [
        0.0,
        0.0,
        0.0,
    ]
    assert config["tool"]["payload_mass_kg"] == 0.07
    assert config["tool"]["payload_com_tool_m"] == [0.0, 0.0, 0.0]
    assert config["planning"]["cut_visual_half_span_mm"] > 0
    assert config["alignment"]["require_same_camera_calibration_revision"] is True
    assert config["alignment"]["fixed_camera"] is True
    assert (
        config["alignment"]["stop_local_vio_after_transform_lock"]
        is True
    )
    assert config["tool"]["observation_registration"]["acting_point_from_tip_mm"] == 50.0
    assert config["handoff"]["minimum_approach_board_offset_mm"] == 60.0
    assert config["handoff"]["approach_clearance_above_vegetable_mm"] == 20.0
    assert config["gui"]["alignment_url"] == "http://127.0.0.1:8011"
    assert (
        config["handoff"]["first_cut_alignment"]["maximum_translation_mm"]
        == 500.0
    )
    assert (
        config["handoff"]["first_cut_alignment"]["minimum_vlm_confidence"]
        == 0.5
    )
    assert (
        config["tool"]["observation_registration"]["consistency"][
            "required_observations"
        ]
        == 1
    )
    assert (
        config["tool"]["observation_registration"][
            "maximum_tool_to_acting_point_m"
        ]
        == 0.8
    )
    assert (
        config["tool"]["observation_registration"][
            "maximum_lateral_offset_magnitude_mm"
        ]
        == 500.0
    )
    assert (
        config["tool"]["observation_registration"][
            "maximum_lateral_offset_disagreement_mm"
        ]
        == 500.0
    )


def test_config_rejects_zero_observation_registration() -> None:
    config = copy.deepcopy(load_skill_config())
    config["tool"]["observation_registration"]["consistency"][
        "required_observations"
    ] = 0
    with pytest.raises(ValueError):
        validate_config(config)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("handoff", "dry_run_required"), False),
        (("handoff", "operator_takeover_required"), False),
        (("handoff", "allow_below_board_target"), True),
        (("tracking", "vlm_after_each_cut"), True),
        (("alignment", "require_same_camera_calibration_revision"), False),
        (("alignment", "fixed_camera"), False),
        (("alignment", "stop_local_vio_after_transform_lock"), False),
        (("handoff", "motion_backend"), "TRANSIT_SPEED"),
        (("tool", "registration_mode"), "VLM_RGBD"),
        (("tool", "vlm_blade_registration_optional"), False),
        (("gui", "alignment_url"), "https://example.com/alignment"),
    ],
)
def test_config_rejects_motion_boundary_relaxation(
    path: tuple[str, str],
    value: object,
) -> None:
    config = copy.deepcopy(load_skill_config())
    config[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        validate_config(config)
