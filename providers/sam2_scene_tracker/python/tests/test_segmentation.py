from __future__ import annotations

import numpy as np

from sam2_scene_tracker.policy import parse_policy
from sam2_scene_tracker.segmentation import (
    constrain_mask_to_prompted_depth_component,
    erode_mask_by_metric_boundary,
    partition_semantic_masks,
    project_masked_depth_to_frame,
)


def _policy():
    return parse_policy(
        {
            "contract_version": 1,
            "policy_id": "test",
            "objects": [
                {
                    "object_id": "table",
                    "type": "KEEP_OUT",
                    "description": "the table",
                }
            ],
        }
    )


def test_arm_mask_and_dilation_remove_arm_pixels_from_obstacle() -> None:
    table = np.ones((9, 9), dtype=bool)
    arm = np.zeros((9, 9), dtype=bool)
    arm[4, 4] = True
    partition = partition_semantic_masks(
        policy=_policy(),
        declared_masks={"table": table},
        arm_mask=arm,
        valid_depth_mask=np.ones((9, 9), dtype=bool),
        arm_dilation_pixels=1,
    )

    assert not partition.object_masks["table"][4, 4]
    assert not partition.object_masks["table"][3, 4]
    assert not partition.object_masks["table"][4, 3]
    assert not partition.object_masks["table"][4, 5]
    assert not partition.object_masks["table"][5, 4]
    assert partition.object_masks["table"][3, 3]
    assert partition.diagnostics["dilated_arm_mask_pixels"] == 5
    assert not partition.pushable_mask.any()


def test_vlm_box_and_depth_connectivity_remove_floor_and_raised_workpiece() -> None:
    depth = np.full((20, 30), 0.9, dtype=np.float32)
    depth[8:, :] = 0.6
    depth[11:16, 12:18] = 0.45
    mask = np.ones(depth.shape, dtype=bool)

    constrained, diagnostics = constrain_mask_to_prompted_depth_component(
        mask=mask,
        depth_m=depth,
        boxes_yxyx=[(350, 0, 1000, 1000)],
        positive_points_yx=[(800, 200), (800, 800)],
        local_depth_step_m=0.035,
    )

    assert not constrained[:8, :].any()
    assert constrained[9, 5]
    assert not constrained[11:16, 12:18].any()
    assert diagnostics["depth_connected_pixels"] < diagnostics[
        "vlm_box_bounded_pixels"
    ]


def test_unclaimed_visible_pixels_default_to_pushable() -> None:
    table = np.zeros((6, 6), dtype=bool)
    table[:, :2] = True
    arm = np.zeros((6, 6), dtype=bool)
    arm[:, 2] = True
    partition = partition_semantic_masks(
        policy=_policy(),
        declared_masks={"table": table},
        arm_mask=arm,
        valid_depth_mask=np.ones((6, 6), dtype=bool),
        arm_dilation_pixels=0,
    )

    assert partition.object_masks["table"].sum() == 12
    assert partition.pushable_mask.sum() == 18


def test_metric_mask_erosion_projects_metres_to_pixels_at_registered_depth() -> None:
    mask = np.zeros((11, 11), dtype=bool)
    mask[2:9, 2:9] = True
    depth = np.ones(mask.shape, dtype=np.float32)
    intrinsics = {"fx": 100.0, "fy": 100.0}

    work_object, work_diagnostics = erode_mask_by_metric_boundary(
        mask=mask,
        depth_m=depth,
        intrinsics=intrinsics,
        erosion_m=0.01,
    )
    keep_out, obstacle_diagnostics = erode_mask_by_metric_boundary(
        mask=mask,
        depth_m=depth,
        intrinsics=intrinsics,
        erosion_m=0.02,
    )

    assert work_object.sum() == 25
    assert keep_out.sum() == 9
    assert work_diagnostics["erosion_radius_px"] == 1
    assert obstacle_diagnostics["erosion_radius_px"] == 2


def test_metric_mask_erosion_uses_one_radius_from_mean_registered_depth() -> None:
    mask = np.zeros((9, 20), dtype=bool)
    mask[1:8, 1:8] = True
    mask[1:8, 12:19] = True
    depth = np.ones(mask.shape, dtype=np.float32)
    depth[:, :10] = 0.5

    eroded, diagnostics = erode_mask_by_metric_boundary(
        mask=mask,
        depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0},
        erosion_m=0.01,
    )

    assert eroded[:, :10].sum() == 25
    assert eroded[:, 10:].sum() == 25
    assert diagnostics["input_mask_pixels"] == 98
    assert diagnostics["output_mask_pixels"] == 50
    assert diagnostics["mean_depth_m"] == 0.75
    assert diagnostics["erosion_radius_px"] == 1


def test_masked_depth_projects_with_camera_transform() -> None:
    depth = np.ones((3, 3), dtype=np.float32)
    mask = np.zeros((3, 3), dtype=bool)
    mask[1, 2] = True
    points = project_masked_depth_to_frame(
        depth_m=depth,
        mask=mask,
        intrinsics={"fx": 1.0, "fy": 1.0, "cx": 1.0, "cy": 1.0},
        target_from_camera={
            "translation_m": [1.0, 2.0, 3.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        pixel_stride=1,
    )

    np.testing.assert_allclose(points, [[2.0, 2.0, 4.0]])


def test_masked_depth_projection_applies_quaternion_rotation() -> None:
    depth = np.ones((1, 2), dtype=np.float32)
    mask = np.asarray([[False, True]])
    points = project_masked_depth_to_frame(
        depth_m=depth,
        mask=mask,
        intrinsics={"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0},
        target_from_camera={
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 2**-0.5, 2**-0.5],
        },
        pixel_stride=1,
    )

    np.testing.assert_allclose(points, [[0.0, 1.0, 1.0]], atol=1e-12)
