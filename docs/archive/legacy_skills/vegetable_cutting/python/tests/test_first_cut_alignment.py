from __future__ import annotations

import numpy as np
import pytest

from vegetable_cutting.first_cut_alignment import (
    build_first_cut_alignment_contract,
    build_first_cut_alignment_correction,
    build_first_cut_pixel_servo_measurement,
)


def config() -> dict[str, float | bool]:
    return {
        "minimum_vlm_confidence": 0.8,
        "no_correction_tolerance_mm": 3.0,
        "maximum_translation_mm": 80.0,
        "no_correction_rotation_tolerance_deg": 1.0,
        "maximum_rotation_deg": 20.0,
        "operator_confirmation_required": True,
    }


def observation(
    *,
    translation_mm: list[float] | None = None,
    rotation_deg: list[float] | None = None,
    meaningful: bool = False,
    person_visible: bool = False,
    orange_target_matches: bool = True,
) -> dict[str, object]:
    translation = translation_mm or [10.0, -2.0, 3.0]
    rotation = rotation_deg or [2.0, -1.0, 4.0]
    return {
        "blade_and_target_visible": True,
        "depth_evidence_used": True,
        "orange_cut_target_matches_vegetable": orange_target_matches,
        "image_plane_alignment_meaningful": meaningful,
        "depth_alignment_meaningful": meaningful,
        "translation_offset_arm_base_mm": dict(
            zip(("x", "y", "z"), translation, strict=True)
        ),
        "rotation_offset_arm_base_deg": dict(
            zip(("roll", "pitch", "yaw"), rotation, strict=True)
        ),
        "meaningful_without_correction": meaningful,
        "person_or_animal_visible_in_workspace": person_visible,
        "confidence": 0.95,
        "notes": "synthetic observation",
    }


def test_pixel_servo_derives_metric_correction_from_observed_blade_point() -> None:
    result = build_first_cut_pixel_servo_measurement(
        {"blade_controlled_point_yx_1000": [500, 250]},
        image_shape=(101, 201, 3),
        intrinsics={"fx": 200.0, "fy": 200.0, "cx": 100.0, "cy": 50.0},
        target_camera_m=[0.0, 0.0, 0.8],
        no_correction_tolerance_mm=3.0,
    )

    assert result["blade_controlled_point_pixel_yx"] == [50, 50]
    assert result["target_controlled_point_pixel_yx"] == [50, 100]
    assert result["translation_offset_camera_mm"] == pytest.approx(
        {"x": 200.0, "y": 0.0, "z": 0.0}
    )
    assert result["meaningful_without_correction"] is False


def test_first_cut_contract_is_review_only() -> None:
    contract = build_first_cut_alignment_contract(config())
    assert contract["status"] == "HUMAN_REVIEW_THEN_BOUNDED_VLM_VISUAL_SERVO"
    assert contract["motion_usable"] is False
    assert contract["correction_semantics"] == (
        "BOUNDED_CAMERA_PIXEL_SERVO_TO_ARM_BASE_TRANSLATION"
    )
    assert (
        contract["vlm_output_semantics"]
        == "OBSERVED_BLADE_CONTROLLED_POINT_PIXEL"
    )
    assert contract["operator_choices"] == [
        "YES",
        "NO_READJUST",
        "FULL_STOP_GO_HOME",
    ]


def test_alignment_returns_bounded_6d_offset() -> None:
    result = build_first_cut_alignment_correction(observation(), config())
    assert result["status"] == "CORRECTION_REVIEW_REQUIRED"
    assert result["translation_arm_base_m"] == pytest.approx(
        [0.01, -0.002, 0.003]
    )
    assert result["rotation_offset_rpy_rad"] == pytest.approx(
        np.deg2rad([2.0, -1.0, 4.0])
    )
    assert result["motion_usable"] is False


def test_alignment_rejects_workspace_person_and_large_offset() -> None:
    unsafe = build_first_cut_alignment_correction(
        observation(person_visible=True),
        config(),
    )
    assert unsafe["status"] == "REJECTED_OBSERVATION"
    assert any("person or animal" in reason for reason in unsafe["quality_reasons"])

    too_large = build_first_cut_alignment_correction(
        observation(translation_mm=[100.0, 0.0, 0.0]),
        config(),
    )
    assert too_large["status"] == "REJECTED_OBSERVATION"
    assert any(
        "translation exceeds" in reason
        for reason in too_large["quality_reasons"]
    )


def test_orange_target_match_is_advisory_for_human_review() -> None:
    result = build_first_cut_alignment_correction(
        observation(orange_target_matches=False),
        config(),
    )

    assert result["status"] == "CORRECTION_REVIEW_REQUIRED"
    assert result["quality_reasons"] == []
    assert result["orange_cut_target_matches_vegetable"] is False
    assert any(
        "human first-cut review remains authoritative" in advisory
        for advisory in result["advisories"]
    )


def test_alignment_rejects_conflicting_meaningful_flag() -> None:
    result = build_first_cut_alignment_correction(
        observation(meaningful=True),
        config(),
    )
    assert result["status"] == "REJECTED_OBSERVATION"
    assert any("conflicts" in reason for reason in result["quality_reasons"])
