from __future__ import annotations

import numpy as np

from sam2_scene_tracker.angular_geometry import (
    ANGULAR_PROFILE_ID,
    ANGULAR_ROI_SCOPE,
    build_hand_angular_assertions,
    build_visible_surface_aabb,
    hand_angular_projection_metadata,
    nearest_spherical_fibonacci_indices,
    spherical_fibonacci_directions,
)


def test_fibonacci_profile_has_uniform_height_spacing_without_polar_crowding() -> None:
    directions = spherical_fibonacci_directions(4096)

    assert directions.shape == (4096, 3)
    assert np.allclose(np.linalg.norm(directions, axis=1), 1.0)
    assert np.allclose(np.diff(directions[:, 2]), -2.0 / 4096.0)
    assert directions[0, 2] < 1.0
    assert directions[-1, 2] > -1.0


def test_constant_time_inverse_matches_exact_nearest_fibonacci_direction() -> None:
    random = np.random.default_rng(20260813)
    vectors = random.normal(size=(512, 3))
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    directions = spherical_fibonacci_directions(4096)

    expected = np.argmax(vectors @ directions.T, axis=1)
    actual = nearest_spherical_fibonacci_indices(vectors, 4096)

    assert np.array_equal(actual, expected)


def test_hand_angular_projection_keeps_nearest_hit_and_grows_with_range() -> None:
    assertions = build_hand_angular_assertions(
        [
            {
                "object_id": "fixture",
                "type": "KEEP_OUT",
                "description": "the fixture",
                "points_m": np.asarray(
                    [
                        [0.10, 0.0, 0.0],
                        [0.20, 0.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ]
                ),
            }
        ],
        hand_center_m=np.zeros(3),
        direction_count=4096,
        minimum_radius_m=0.001,
        radial_padding_m=0.0,
    )

    assert len(assertions) == 2
    assert {value["roi_scope"] for value in assertions} == {ANGULAR_ROI_SCOPE}
    radii = sorted(float(value["radius_m"]) for value in assertions)
    assert radii[1] > radii[0] * 9.0
    projection = hand_angular_projection_metadata(
        hand_center_m=np.zeros(3),
        observed_at_us=2_000_000,
        direction_count=4096,
        occupied_direction_count=len(assertions),
        angular_radius_scale=1.5,
        minimum_radius_m=0.005,
        radial_padding_m=0.003,
        maximum_range_m=1.2,
    )
    assert projection["profile_id"] == ANGULAR_PROFILE_ID
    assert projection["origin_m"] == [0.0, 0.0, 0.0]
    assert projection["occupied_direction_count"] == 2
    assert projection["keep_out_boundary_mode"] == "HAND_RAY_TANGENT"


def test_hand_angular_projection_has_fixed_directional_upper_bound() -> None:
    random = np.random.default_rng(42)
    points = random.normal(size=(20_000, 3))
    points /= np.linalg.norm(points, axis=1)[:, None]
    points *= random.uniform(0.05, 1.0, size=(points.shape[0], 1))

    assertions = build_hand_angular_assertions(
        [
            {
                "object_id": "workspace",
                "type": "KEEP_OUT",
                "description": "the workspace obstacle",
                "points_m": points,
            }
        ],
        hand_center_m=np.zeros(3),
        direction_count=4096,
    )

    assert 0 < len(assertions) <= 4096
    assert len({value["sphere_id"] for value in assertions}) == len(assertions)


def test_visible_surface_aabb_uses_arm_base_semantic_corner_names() -> None:
    aabb = build_visible_surface_aabb(
        object_id="workpiece",
        object_type="WORK_OBJECT",
        description="the deformable workpiece",
        points_m=np.asarray(
            [
                [0.20, -0.10, 0.01],
                [0.50, 0.30, 0.25],
                [0.35, 0.00, 0.12],
            ]
        ),
        observed_at_us=2_000_000,
        freshness_ms=5000,
        source_frame_number=17,
        source_policy_revision="policy-2",
    )

    assert aabb is not None
    assert aabb["frame_id"] == "rebot_arm_base"
    assert aabb["expires_at_us"] == 7_000_000
    assert aabb["corners_m"]["right_forward_up"] == [0.5, -0.1, 0.25]
    assert aabb["axis_semantics"]["right"] == "-Y"
    assert aabb["axis_semantics"]["forward"] == "+X"


def test_visible_surface_aabb_rejects_obstacles() -> None:
    try:
        build_visible_surface_aabb(
            object_id="table",
            object_type="KEEP_OUT",
            description="the table",
            points_m=np.asarray([[0.2, 0.0, 0.1]]),
            observed_at_us=2_000_000,
        )
    except ValueError as error:
        assert "only valid for WORK_OBJECT" in str(error)
    else:
        raise AssertionError("obstacle AABB was accepted")
