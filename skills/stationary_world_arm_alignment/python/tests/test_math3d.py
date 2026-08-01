from __future__ import annotations

import json

import numpy as np
import pytest

from stationary_world_arm_alignment.math3d import (
    apply_base_mesh_hypothesis_correction,
    closest_pair_consensus,
    inspect_base_up_alignment,
    robust_average_transforms,
    select_base_orientation_correction,
    select_base_orientation_from_gripper_point,
    transform_matrix,
    transform_from_payload,
    transform_payload,
)


def test_transform_payload_round_trip_shape() -> None:
    value = transform_matrix([1, 2, 3], [0, 0, 0, 1])
    payload = transform_payload(value)
    assert payload["translation_m"] == [1.0, 2.0, 3.0]
    assert np.allclose(payload["rotation_xyzw"], [0, 0, 0, 1])


def test_transform_payload_round_trip_preserves_parent_from_child_rotation() -> None:
    value = transform_matrix(
        [0.3, -0.8, 1.2],
        [0.20137403, -0.30206105, 0.10068702, 0.92632054],
    )

    reconstructed = transform_from_payload(transform_payload(value))

    assert np.allclose(reconstructed, value, rtol=0.0, atol=1e-9)


def test_robust_average_rejects_large_translation_outlier() -> None:
    values = [
        transform_matrix([0.001 * index, 0, 1], [0, 0, 0, 1])
        for index in range(6)
    ]
    values.append(transform_matrix([1.5, -2, 3], [0, 0, 0, 1]))
    average, diagnostics = robust_average_transforms(values)
    assert abs(average[0, 3] - 0.0025) < 0.005
    assert diagnostics["retained_count"] == 6


@pytest.mark.parametrize(
    (
        "raw_rotation",
        "relation",
        "expected_axis",
        "expected_yaw_flip",
    ),
    [
        (np.eye(3), "TOWARD_GRIPPER", "NONE", 0),
        (np.eye(3), "AWAY_FROM_GRIPPER", "Z", 180),
        (np.diag([1.0, -1.0, -1.0]), "TOWARD_GRIPPER", "X", 0),
        (np.diag([1.0, -1.0, -1.0]), "AWAY_FROM_GRIPPER", "Y", 180),
    ],
)
def test_base_orientation_uses_at_most_one_principal_axis_rotation(
    raw_rotation: np.ndarray,
    relation: str,
    expected_axis: str,
    expected_yaw_flip: int,
) -> None:
    camera_from_base = np.eye(4, dtype=np.float64)
    camera_from_base[:3, :3] = raw_rotation
    camera_from_base[:3, 3] = [0.4, -0.2, 1.0]
    original_translation = camera_from_base[:3, 3].copy()

    correction, diagnostics = select_base_orientation_correction(
        camera_from_base,
        [0.0, 0.0, 1.0],
        relation,
    )
    corrected = camera_from_base @ correction

    assert diagnostics["selected_orientation_correction_axis"] == expected_axis
    assert diagnostics["selected_flip_deg"] == expected_yaw_flip
    assert diagnostics["orientation_correction_count"] == (
        0 if expected_axis == "NONE" else 1
    )
    assert diagnostics["orientation_correction_translation_norm_m"] == 0.0
    assert diagnostics["yaw_correction_translation_norm_m"] == 0.0
    assert diagnostics["corrected_base_z_dot_world_up"] == pytest.approx(1.0)
    assert np.array_equal(corrected[:3, 3], original_translation)
    assert np.linalg.det(correction[:3, :3]) == pytest.approx(1.0)
    assert np.array_equal(
        correction[:3, :3].T @ correction[:3, :3],
        np.eye(3),
    )
    json.dumps(diagnostics)


def test_base_orientation_rejects_unknown_gripper_relation() -> None:
    with pytest.raises(ValueError, match="base_x_relation_to_gripper"):
        select_base_orientation_correction(
            np.eye(4),
            [0.0, 0.0, 1.0],
            "LEFT",
        )


def test_rgbd_gripper_and_downward_z_select_one_y_rotation() -> None:
    camera_from_base = np.eye(4, dtype=np.float64)
    camera_from_base[:3, :3] = np.diag([1.0, -1.0, -1.0])
    camera_from_base[:3, 3] = [0.4, -0.2, 1.0]
    raw_base_gripper = np.array([-0.3, 0.02, 0.35, 1.0])
    camera_gripper = (camera_from_base @ raw_base_gripper)[:3]

    correction, review, diagnostics = (
        select_base_orientation_from_gripper_point(
            camera_from_base,
            [0.0, 0.0, 1.0],
            camera_gripper,
        )
    )
    corrected = camera_from_base @ correction
    corrected_base_gripper = (
        np.linalg.inv(corrected)
        @ np.append(camera_gripper, 1.0)
    )[:3]

    assert review["base_x_relation_to_gripper"] == "AWAY_FROM_GRIPPER"
    assert diagnostics["selected_orientation_correction_axis"] == "Y"
    assert diagnostics["orientation_correction_count"] == 1
    assert diagnostics["selected_flip_deg"] == 180
    assert diagnostics["corrected_base_z_dot_world_up"] == pytest.approx(1.0)
    assert corrected_base_gripper[0] > 0.0
    assert np.array_equal(corrected[:3, 3], camera_from_base[:3, 3])


def test_downward_mesh_hypothesis_is_fixed_before_semantic_root_offset() -> None:
    mesh_from_base = np.eye(4, dtype=np.float64)
    mesh_from_base[2, 3] = -0.0446249945
    parent_from_mesh = np.eye(4, dtype=np.float64)
    parent_from_mesh[:3, :3] = np.diag([1.0, -1.0, -1.0])
    parent_from_mesh[:3, 3] = [1.0, 2.0, 3.0]
    raw_parent_from_base = parent_from_mesh @ mesh_from_base
    correction = np.eye(4, dtype=np.float64)
    correction[:3, :3] = np.diag([1.0, -1.0, -1.0])

    corrected, diagnostics = apply_base_mesh_hypothesis_correction(
        raw_parent_from_base,
        mesh_from_base,
        correction,
    )

    corrected_parent_from_mesh = corrected @ np.linalg.inv(mesh_from_base)
    assert np.allclose(
        corrected_parent_from_mesh[:3, 3],
        parent_from_mesh[:3, 3],
        rtol=0.0,
        atol=1e-12,
    )
    assert np.allclose(corrected[:3, :3], np.eye(3), atol=1e-12)
    assert corrected[2, 3] == pytest.approx(3.0 - 0.0446249945)
    assert raw_parent_from_base[2, 3] == pytest.approx(
        3.0 + 0.0446249945
    )
    assert diagnostics["mesh_center_translation_preserved"] is True
    assert diagnostics[
        "mesh_hypothesis_correction_translation_norm_m"
    ] == 0.0
    assert diagnostics[
        "semantic_root_translation_adjustment_norm_m"
    ] == pytest.approx(2.0 * 0.0446249945)


def test_root_z_hypothesis_keeps_semantic_root_translation() -> None:
    mesh_from_base = np.eye(4, dtype=np.float64)
    mesh_from_base[2, 3] = -0.0446249945
    parent_from_base = np.eye(4, dtype=np.float64)
    parent_from_base[:3, 3] = [0.4, -0.2, 1.0]
    correction = np.eye(4, dtype=np.float64)
    correction[:3, :3] = np.diag([-1.0, -1.0, 1.0])

    corrected, diagnostics = apply_base_mesh_hypothesis_correction(
        parent_from_base,
        mesh_from_base,
        correction,
    )

    assert np.allclose(
        corrected[:3, 3],
        parent_from_base[:3, 3],
        rtol=0.0,
        atol=1e-12,
    )
    assert diagnostics[
        "semantic_root_translation_adjustment_norm_m"
    ] == pytest.approx(0.0, abs=1e-12)


def test_recorded_downward_pose_becomes_up_with_one_mesh_correction() -> None:
    raw_parent_from_base = transform_matrix(
        [1.2022283473349376, -0.028860280991788084, -0.5905461427423252],
        [
            0.0289494167613269,
            0.9992611410688954,
            0.01927962755406601,
            0.01635234479347973,
        ],
    )
    raw_gripper_in_base = np.array(
        [0.15816896553409165, 0.0005816202579091954, -0.1744809900375366, 1.0]
    )
    parent_gripper = (raw_parent_from_base @ raw_gripper_in_base)[:3]
    mesh_from_base = np.eye(4, dtype=np.float64)
    mesh_from_base[2, 3] = -0.0446249945

    correction, review, selection = (
        select_base_orientation_from_gripper_point(
            raw_parent_from_base,
            [0.0, 0.0, 1.0],
            parent_gripper,
        )
    )
    corrected, application = apply_base_mesh_hypothesis_correction(
        raw_parent_from_base,
        mesh_from_base,
        correction,
    )
    corrected_gripper_in_base = (
        np.linalg.inv(corrected) @ np.append(parent_gripper, 1.0)
    )[:3]

    assert review["base_x_relation_to_gripper"] == "TOWARD_GRIPPER"
    assert selection["selected_orientation_correction_axis"] == "X"
    assert selection["orientation_correction_count"] == 1
    assert selection["raw_base_z_dot_world_up"] == pytest.approx(
        -0.9987217935622634
    )
    assert float(corrected[2, 2]) == pytest.approx(0.9987217935622634)
    assert corrected_gripper_in_base[0] > 0.0
    assert corrected_gripper_in_base[2] > 0.0
    assert application["mesh_center_translation_preserved"] is True
    assert application[
        "semantic_root_translation_adjustment_norm_m"
    ] == pytest.approx(0.089249989)


def test_rgbd_gripper_point_reports_weak_yaw_leverage_as_warning() -> None:
    _, _, diagnostics = select_base_orientation_from_gripper_point(
        np.eye(4),
        [0.0, 0.0, 1.0],
        [0.001, 0.001, 0.4],
    )

    assert diagnostics["weak_geometry"] is True
    assert "weak horizontal yaw leverage" in diagnostics["warning"]


def test_base_up_alignment_reports_aligned_without_modification() -> None:
    camera_from_base = transform_matrix(
        [0.1, -0.2, 1.1],
        [0.0, 0.0, 0.0, 1.0],
    )
    original = camera_from_base.copy()

    diagnostics = inspect_base_up_alignment(
        camera_from_base,
        camera_system_up=np.array([0.0, 0.0, 4.0]),
        warning_tilt_deg=10.0,
    )

    assert diagnostics["status"] == "ALIGNED"
    assert diagnostics["world_up_available"] is True
    assert diagnostics["within_warning_tolerance"] is True
    assert diagnostics["base_z_tilt_from_world_up_deg"] == 0.0
    assert diagnostics["warning"] is None
    assert diagnostics["transform_modified"] is False
    assert np.array_equal(camera_from_base, original)


def test_base_up_alignment_retains_tilted_pose_as_warning() -> None:
    camera_from_base = np.eye(4, dtype=np.float64)
    camera_from_base[:3, 2] = [0.0, 1.0, 0.0]

    diagnostics = inspect_base_up_alignment(
        camera_from_base,
        camera_system_up=np.array([0.0, 0.0, 1.0]),
        warning_tilt_deg=10.0,
    )

    assert diagnostics["status"] == "TILT_WARNING"
    assert diagnostics["within_warning_tolerance"] is False
    assert diagnostics["base_z_tilt_from_world_up_deg"] == pytest.approx(90.0)
    assert "retained without modification" in diagnostics["warning"]
    assert diagnostics["transform_modified"] is False


@pytest.mark.parametrize(
    "camera_system_up, expected_text",
    [
        (None, "unavailable"),
        (np.zeros(3), "invalid"),
    ],
)
def test_base_up_alignment_missing_or_invalid_up_is_warning(
    camera_system_up: np.ndarray | None,
    expected_text: str,
) -> None:
    diagnostics = inspect_base_up_alignment(
        np.eye(4, dtype=np.float64),
        camera_system_up=camera_system_up,
    )

    assert diagnostics["status"] == "WORLD_UP_UNAVAILABLE"
    assert diagnostics["world_up_available"] is False
    assert diagnostics["within_warning_tolerance"] is None
    assert expected_text in diagnostics["warning"].lower()
    assert diagnostics["transform_modified"] is False


def test_closest_pair_consensus_discards_translation_outlier() -> None:
    consensus, diagnostics = closest_pair_consensus(
        [
            [0.10, 0.20, 0.30],
            [0.11, 0.19, 0.31],
            [0.80, -0.50, 1.20],
        ]
    )

    assert diagnostics["selected_indices"] == [0, 1]
    assert np.allclose(consensus, [0.105, 0.195, 0.305])
