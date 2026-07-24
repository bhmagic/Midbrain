from __future__ import annotations

import math

import numpy as np

from stationary_world_arm_alignment.math3d import (
    base_upright_correction,
    choose_base_symmetry,
    closest_pair_consensus,
    robust_average_transforms,
    transform_matrix,
    transform_payload,
)


def test_transform_payload_round_trip_shape() -> None:
    value = transform_matrix([1, 2, 3], [0, 0, 0, 1])
    payload = transform_payload(value)
    assert payload["translation_m"] == [1.0, 2.0, 3.0]
    assert np.allclose(payload["rotation_xyzw"], [0, 0, 0, 1])


def test_robust_average_rejects_large_translation_outlier() -> None:
    values = [
        transform_matrix([0.001 * index, 0, 1], [0, 0, 0, 1])
        for index in range(6)
    ]
    values.append(transform_matrix([1.5, -2, 3], [0, 0, 0, 1]))
    average, diagnostics = robust_average_transforms(values)
    assert abs(average[0, 3] - 0.0025) < 0.005
    assert diagnostics["retained_count"] == 6


def test_symmetry_uses_tool_point_to_select_positive_x() -> None:
    base = transform_matrix([0, 0, 0], [0, 0, 0, 1])
    selected, diagnostics = choose_base_symmetry(
        base,
        np.array([0.5, 0, 0]),
        base_tool_point=np.array([0.4, 0, 0]),
    )
    assert diagnostics["selected_flip_deg"] == 0
    assert math.isclose(selected[0, 0], 1.0)


def test_upside_down_base_is_corrected_to_vio_world_up() -> None:
    upside_down = np.eye(4, dtype=np.float64)
    upside_down[:3, :3] = np.array(
        [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
    )
    correction, diagnostics = base_upright_correction(upside_down)
    corrected = upside_down @ correction

    assert diagnostics["correction"] == "SEMANTIC_X_180"
    assert diagnostics["raw_base_z_dot_world_up"] == -1.0
    assert np.allclose(corrected[:3, 2], [0.0, 1.0, 0.0])


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
