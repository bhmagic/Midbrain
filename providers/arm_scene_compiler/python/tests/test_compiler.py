from __future__ import annotations

import numpy as np
import pytest

from arm_scene_compiler.compiler import (
    ARM_BASE_ROI,
    GRIPPER_ROI,
    HAND_ANGULAR_ROI,
    build_layered_scene,
    build_profile_self_exclusion_spheres,
    build_self_exclusion_spheres,
)


def _self_geometry():
    centers = np.asarray(
        [[0.0, 0.0, index * 0.1] for index in range(8)],
        dtype=np.float64,
    )
    return build_self_exclusion_spheres(
        centers,
        [0.04] * 7,
        maximum_spacing_m=0.025,
    )


def test_layered_scene_enforces_both_roi_policies_and_semantics() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[
            [0.12, 0.0, 0.68],
            [0.82, 0.0, 0.10],
            [1.30, 0.0, 0.0],
        ],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        semantic_objects=[
            {
                "object_id": "toilet-paper",
                "center_m": [0.20, 0.0, 0.68],
                "radius_m": 0.06,
                "type": "WORKPIECE",
            }
        ],
    )

    policies = {value["scope"]: value for value in scene["roi_layers"]}
    assert policies[GRIPPER_ROI]["radius_m"] == 0.5
    assert policies[GRIPPER_ROI]["minimum_sphere_radius_m"] == 0.02
    assert policies[ARM_BASE_ROI]["radius_m"] == 1.2
    assert policies[ARM_BASE_ROI]["minimum_sphere_radius_m"] == 0.06
    workpiece = next(
        value for value in scene["spheres"] if value["object_id"] == "toilet-paper"
    )
    assert workpiece["type"] == "WORK_OBJECT"
    assert workpiece["roi_scope"] == GRIPPER_ROI
    assert scene["production"]["default_unclassified_type"] == "PUSHABLE"
    assert scene["production"]["unclaimed_pushable_geometry_published"] is False
    assert all(
        value["semantic_source"] != "UNCLAIMED_VISIBLE_DEPTH_DEFAULT_PUSHABLE"
        for value in scene["spheres"]
    )


def test_self_filter_removes_robot_geometry_before_voxelization() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[[0.0, 0.0, 0.35], [0.3, 0.0, 0.35]],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        publish_unclaimed_pushable_geometry=True,
    )
    assert scene["production"]["self_points_removed"] == 1
    assert len(scene["spheres"]) == 1
    assert scene["spheres"][0]["type"] == "PUSHABLE"


def test_profile_self_filter_includes_mounted_effector_spheres() -> None:
    centers = np.asarray(
        [[0.0, 0.0, index * 0.1] for index in range(7)],
        dtype=np.float64,
    )
    spheres, revision = build_profile_self_exclusion_spheres(
        centers,
        [0.04] * 6,
        [
            {
                "primitive_id": "rear",
                "center_m": [0.0, 0.0, 0.5],
                "radius_m": 0.035,
            }
        ],
        assembly_fingerprint="assembly-1",
    )

    effector = next(
        value for value in spheres if value.get("primitive_id") == "rear"
    )
    assert effector["sphere_id"] == "self:effector:rear"
    assert effector["geometry_owner"] == "MOUNTED_EFFECTOR_PROFILE"
    assert revision.startswith("self-filter-")


def test_profile_self_filter_removes_sam2_cells_on_effector_geometry() -> None:
    centers = np.asarray(
        [[0.0, 0.0, index * 0.1] for index in range(7)],
        dtype=np.float64,
    )
    self_spheres, revision = build_profile_self_exclusion_spheres(
        centers,
        [0.02] * 6,
        [
            {
                "primitive_id": "rear",
                "center_m": [0.1, 0.0, 0.5],
                "radius_m": 0.035,
            }
        ],
        assembly_fingerprint="assembly-1",
    )
    scene = build_layered_scene(
        raw_points_arm_base_m=[],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        semantic_objects=[
            {
                "sphere_id": "sam2-tool-cell",
                "object_id": "workpiece",
                "center_m": [0.1, 0.0, 0.5],
                "radius_m": 0.02,
                "type": "WORKPIECE",
                "semantic_source": "SAM2_TRACKED_WORKPIECE",
            }
        ],
    )

    assert scene["spheres"] == []
    assert scene["production"]["sam2_semantic_self_cells_removed"] == 1


def test_self_filter_accounts_for_generated_voxel_sphere_radius() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[
            [0.069, 0.0, 0.35],
            [0.071, 0.0, 0.35],
        ],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        self_filter_margin_m=0.01,
        publish_unclaimed_pushable_geometry=True,
    )

    assert scene["production"]["self_points_removed"] == 1
    pushable = scene["spheres"][0]
    assert pushable["type"] == "PUSHABLE"
    assert pushable["radius_m"] == pytest.approx(0.02)
    assert pushable["center_m"][0] == pytest.approx(0.071)


def test_semantic_only_depth_fallback_is_explicit() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        semantic_objects=[
            {
                "object_id": "reflective-workpiece",
                "center_m": [0.2, 0.0, 0.7],
                "radius_m": 0.03,
                "type": "WORK_OBJECT",
            }
        ],
    )
    assert scene["production"]["depth_mode"] == "SEMANTIC_ONLY"
    assert scene["spheres"][0]["type"] == "WORK_OBJECT"


def test_unknown_semantic_type_is_rejected() -> None:
    self_spheres, revision = _self_geometry()
    with pytest.raises(ValueError, match="unsupported semantic object type"):
        build_layered_scene(
            raw_points_arm_base_m=[],
            gripper_center_arm_base_m=[0.0, 0.0, 0.7],
            self_exclusion_spheres=self_spheres,
            self_filter_revision=revision,
            semantic_objects=[
                {
                    "object_id": "unknown",
                    "center_m": [0.2, 0.0, 0.7],
                    "radius_m": 0.03,
                    "type": "MAYBE_PUSHABLE",
                }
            ],
        )


def test_keep_out_requires_an_authoritative_description() -> None:
    self_spheres, revision = _self_geometry()
    with pytest.raises(ValueError, match="requires a user/upstream description"):
        build_layered_scene(
            raw_points_arm_base_m=[],
            gripper_center_arm_base_m=[0.0, 0.0, 0.7],
            self_exclusion_spheres=self_spheres,
            self_filter_revision=revision,
            semantic_objects=[
                {
                    "object_id": "vague-obstacle",
                    "center_m": [0.4, 0.0, 0.1],
                    "radius_m": 0.03,
                    "type": "KEEP_OUT",
                }
            ],
        )


def test_untyped_semantic_geometry_defaults_to_pushable() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        semantic_objects=[
            {
                "object_id": "unclaimed-object",
                "center_m": [0.4, 0.0, 0.1],
                "radius_m": 0.03,
            }
        ],
    )
    assert scene["spheres"][0]["type"] == "PUSHABLE"


def test_hand_angular_spheres_and_visible_aabb_survive_scene_compilation() -> None:
    self_spheres, revision = _self_geometry()
    scene = build_layered_scene(
        raw_points_arm_base_m=[],
        gripper_center_arm_base_m=[0.0, 0.0, 0.7],
        self_exclusion_spheres=self_spheres,
        self_filter_revision=revision,
        semantic_objects=[
            {
                "sphere_id": "sam2:HAND_ANGULAR_4PI:7",
                "object_id": "workpiece",
                "description": "the workpiece",
                "center_m": [0.8, 0.0, 0.2],
                "radius_m": 0.01,
                "type": "WORK_OBJECT",
                "roi_scope": HAND_ANGULAR_ROI,
                "semantic_source": "SAM2_TRACKED_USER_DECLARED",
                "angular_profile": "SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1",
                "angular_bin_index": 7,
            }
        ],
        semantic_aabbs=[
            {
                "extent_kind": "VISIBLE_SURFACE_AABB",
                "object_id": "workpiece",
                "description": "the workpiece",
                "type": "WORK_OBJECT",
                "frame_id": "rebot_arm_base",
                "observed_at_us": 1_000_000,
                "freshness_ms": 5000,
                "expires_at_us": 6_000_000,
                "minimum_m": [0.5, -0.2, 0.1],
                "maximum_m": [0.8, 0.1, 0.3],
            }
        ],
        semantic_angular_projection={
            "profile_id": "SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1",
            "roi_scope": "HAND_ANGULAR_4PI",
            "origin_frame_id": "rebot_arm_base",
            "origin_m": [0.1, 0.0, 0.7],
            "observed_at_us": 1_000_000,
            "direction_count": 4096,
            "occupied_direction_count": 1,
        },
    )

    angular_layer = next(
        value for value in scene["roi_layers"] if value["scope"] == HAND_ANGULAR_ROI
    )
    assert angular_layer["minimum_sphere_radius_m"] == 0.005
    assert angular_layer["center_m"] == [0.1, 0.0, 0.7]
    assert angular_layer["projection"]["direction_count"] == 4096
    assert scene["spheres"][0]["radius_m"] == 0.01
    assert scene["spheres"][0]["angular_bin_index"] == 7
    aabb = scene["visible_surface_aabbs"][0]
    assert aabb["center_m"] == [0.65, -0.05, 0.2]
    assert aabb["corners_m"]["right_forward_up"] == [0.8, -0.2, 0.3]
