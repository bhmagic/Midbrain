from __future__ import annotations

import numpy as np

from sam2_scene_tracker.fusion import PersistentSemanticVoxelMap


def test_repeated_frames_merge_without_duplicate_assertions() -> None:
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    semantic_map.bind_identity("epoch-1:cal-1:policy-1")
    points = np.asarray(
        [[0.30, 0.00, 0.02], [0.301, 0.001, 0.021]], dtype=np.float64
    )
    first = semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=points,
        observed_at_us=1,
    )
    second = semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=points + 0.001,
        observed_at_us=2,
    )

    assert first["persistent_voxel_count"] == 1
    assert first["frame_voxel_count"] == 1
    assert second["persistent_voxel_count"] == 1
    assertions = semantic_map.assertions(
        gripper_center_m=np.asarray([0.25, 0.0, 0.2]),
    )
    assert len(assertions) == 1
    assert assertions[0]["object_id"] == "table"
    assert assertions[0]["type"] == "KEEP_OUT"


def test_occlusion_retains_existing_voxels() -> None:
    semantic_map = PersistentSemanticVoxelMap()
    semantic_map.bind_identity("identity-1")
    semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=np.asarray([[0.4, 0.0, 0.02]]),
        observed_at_us=1,
    )
    semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=np.empty((0, 3)),
        observed_at_us=2,
    )

    assert semantic_map.snapshot()["objects"]["table"]["persistent_voxel_count"] == 1


def test_epoch_or_calibration_identity_change_clears_permanent_map() -> None:
    semantic_map = PersistentSemanticVoxelMap()
    semantic_map.bind_identity("epoch-1:cal-1:policy-1")
    semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=np.asarray([[0.4, 0.0, 0.02]]),
        observed_at_us=1,
    )

    assert semantic_map.bind_identity("epoch-2:cal-1:policy-1")
    assert semantic_map.snapshot()["objects"] == {}


def test_dense_frame_is_reduced_before_persistent_neighbor_matching() -> None:
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    semantic_map.bind_identity("identity-1")
    axis = np.linspace(0.0, 0.019, 100)
    points = np.column_stack(
        [axis.repeat(100), np.tile(axis, 100), np.full(10_000, 0.02)]
    )

    result = semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=points,
        observed_at_us=1,
    )

    assert result["input_points"] == 10_000
    assert result["frame_voxel_count"] <= 2
    assert result["persistent_voxel_count"] <= 2


def test_keep_out_spheres_are_tangent_behind_a_planar_visible_surface() -> None:
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    semantic_map.bind_identity("identity-1")
    x, y = np.meshgrid(
        np.linspace(0.20, 0.44, 13),
        np.linspace(-0.12, 0.12, 13),
    )
    points = np.column_stack((x.ravel(), y.ravel(), np.zeros(x.size)))
    semantic_map.update(
        object_id="table",
        object_type="KEEP_OUT",
        description="the table",
        points_m=points,
        observed_at_us=1,
        surface_viewpoint_m=np.asarray([0.0, 0.0, 1.0]),
    )

    assertions = semantic_map.assertions(
        gripper_center_m=np.asarray([1.0, 0.0, 0.5]),
    )

    assert assertions
    assert {
        value["surface_boundary_mode"] for value in assertions
    } == {"DOMINANT_PLANE_TANGENT"}
    for value in assertions:
        surface_z = float(value["surface_center_m"][2])
        sphere_top_z = float(value["center_m"][2]) + float(value["radius_m"])
        assert abs(surface_z - sphere_top_z) < 1e-9


def test_keep_out_sphere_uses_view_ray_when_surface_is_not_planar() -> None:
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    semantic_map.bind_identity("identity-1")
    semantic_map.update(
        object_id="fixture",
        object_type="KEEP_OUT",
        description="the fixture",
        points_m=np.asarray([[0.4, 0.0, 0.0]]),
        observed_at_us=1,
        surface_viewpoint_m=np.asarray([0.0, 0.0, 0.0]),
    )

    assertion = semantic_map.assertions(
        gripper_center_m=np.asarray([1.0, 0.0, 0.0]),
    )[0]

    assert assertion["surface_boundary_mode"] == "VIEW_RAY_TANGENT"
    assert np.allclose(assertion["center_m"], [0.46, 0.0, 0.0])
    assert abs(float(assertion["radius_m"]) - 0.06) < 1e-12
