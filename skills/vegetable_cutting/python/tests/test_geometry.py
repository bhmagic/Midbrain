from __future__ import annotations

import numpy as np
import pytest

from vegetable_cutting.geometry import (
    Plane,
    fit_board_plane,
    mask_to_normalized_polygon,
    plan_cut_points_on_line_3d,
    plan_cross_cuts,
    point_in_polygon,
    refine_object_mask_above_plane,
)


def test_two_point_3d_line_planner_ignores_board_shape() -> None:
    plan = plan_cut_points_on_line_3d(
        np.asarray([0.0, 0.0, 0.1]),
        np.asarray([0.10, 0.0, 0.1]),
        spacing_m=0.02,
        maximum_cut_count=20,
    )
    assert plan["source"] == "TWO_RGBD_BOARD_POINTS_STRAIGHT_LINE"
    assert len(plan["cuts"]) == 4
    np.testing.assert_allclose(
        np.asarray([cut["center_m"] for cut in plan["cuts"]]),
        np.asarray(
            [
                [0.02, 0.0, 0.1],
                [0.04, 0.0, 0.1],
                [0.06, 0.0, 0.1],
                [0.08, 0.0, 0.1],
            ]
        ),
    )


def test_fit_board_plane_from_constant_depth() -> None:
    depth = np.ones((120, 160), dtype=np.float32)
    mask = np.zeros_like(depth, dtype=np.uint8)
    mask[10:110, 15:145] = 255
    intrinsics = {"fx": 120.0, "fy": 120.0, "cx": 79.5, "cy": 59.5}
    plane, valid_fraction = fit_board_plane(
        depth,
        intrinsics,
        mask,
        stride_px=2,
        minimum_points=500,
    )
    assert valid_fraction == 1.0
    assert plane.rmse_m < 1e-8
    assert abs(abs(plane.normal[2]) - 1.0) < 1e-8


def test_cross_cut_planner_advances_along_major_axis() -> None:
    board = np.asarray(
        [
            [-0.20, -0.15],
            [0.20, -0.15],
            [0.20, 0.15],
            [-0.20, 0.15],
        ]
    )
    vegetable = np.asarray(
        [
            [-0.10, -0.02],
            [0.10, -0.02],
            [0.10, 0.02],
            [-0.10, 0.02],
        ]
    )
    plan = plan_cross_cuts(
        vegetable,
        board,
        spacing_m=0.05,
        vegetable_end_margin_m=0.01,
        board_entry_exit_margin_m=0.005,
        maximum_cut_count=20,
    )
    cuts = plan["cuts"]
    assert len(cuts) == 4
    assert all(cut["vegetable_width_m"] == pytest.approx(0.04) for cut in cuts)
    assert all(point_in_polygon(np.asarray(cut["entry_uv_m"]), board) for cut in cuts)
    stations = [cut["station_longitudinal_m"] for cut in cuts]
    assert np.diff(stations) == pytest.approx([0.05, 0.05, 0.05])


def test_depth_refinement_recovers_raised_object_boundary() -> None:
    height, width = 120, 160
    depth = np.ones((height, width), dtype=np.float32)
    depth[45:70, 45:115] = 0.96
    board_mask = np.zeros((height, width), dtype=np.uint8)
    board_mask[10:110, 10:150] = 255
    incorrect_vlm_mask = np.zeros((height, width), dtype=np.uint8)
    incorrect_vlm_mask[40:105, 40:85] = 255
    plane = Plane(
        origin_m=np.asarray([0.0, 0.0, 1.0]),
        normal=np.asarray([0.0, 0.0, -1.0]),
        axis_u=np.asarray([1.0, 0.0, 0.0]),
        axis_v=np.asarray([0.0, -1.0, 0.0]),
        rmse_m=0.001,
    )
    intrinsics = {"fx": 120.0, "fy": 120.0, "cx": 79.5, "cy": 59.5}
    refined, diagnostics = refine_object_mask_above_plane(
        depth,
        intrinsics,
        board_mask,
        plane,
        incorrect_vlm_mask,
        minimum_height_m=0.005,
        maximum_height_m=0.15,
        minimum_component_pixels=100,
        minimum_vlm_overlap_pixels=20,
    )
    assert np.count_nonzero(refined[45:70, 45:115]) > 1600
    assert diagnostics["median_height_mm"] == pytest.approx(40.0, abs=0.1)
    polygon = mask_to_normalized_polygon(refined)
    assert len(polygon) >= 4
