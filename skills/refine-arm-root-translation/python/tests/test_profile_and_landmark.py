from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from jsonschema import ValidationError, validate

from refine_arm_root_translation import (
    InvalidDepthSelectionError,
    build_alignment_image_projections,
    build_detection_annotations,
    build_landmark_review_crop_annotations,
    build_landmark_prompt,
    build_visual_annotations,
    canonical_yx_to_pixel,
    load_effector_profile,
    prepare_translation_refinement,
    render_marked_overlap_png,
    render_landmark_review_crop_png,
    resolve_profile_landmark,
    resolve_tool_landmark_point,
    select_visual_landmark,
    validate_landmark_detection,
)


SKILL_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = (
    WORKSPACE_ROOT
    / "providers"
    / "rebot_arm_dm"
    / "profiles"
    / "effectors"
    / "rebot_b601_dm_bare_gripper.v2.json"
)
BLADE_PROFILE_PATH = PROFILE_PATH.with_name("rebot_b601_dm_5_inch_blade.v1.json")


def detection_for(landmark: dict, *, confidence: float = 0.95) -> dict:
    return {
        "schema": "midbrain.effector_landmark_detection",
        "schema_version": 2,
        "scene_suitable": True,
        "landmark_id": landmark["landmark_id"],
        "coordinate_space": "NORMALIZED_YX_0_1000_PER_IMAGE",
        "reason": "Both physical features and their depth surfaces are visible.",
        "points": [
            {
                "point_id": landmark["required_point_ids"][0],
                "rgb_yx_0_1000": [444, 333],
                "registered_depth_yx_0_1000": [500, 400],
                "confidence": confidence,
                "same_surface_confidence": 0.94,
                "reason": "Left physical feature and same-surface depth.",
            },
            {
                "point_id": landmark["required_point_ids"][1],
                "rgb_yx_0_1000": [444, 778],
                "registered_depth_yx_0_1000": [500, 600],
                "confidence": 0.96,
                "same_surface_confidence": 0.93,
                "reason": "Right physical feature and same-surface depth.",
            },
        ],
    }


def test_gripper_profile_uses_measured_rail_center_by_default() -> None:
    profile = load_effector_profile(PROFILE_PATH)

    assert profile["default_visual_alignment_landmark"] == (
        "rail_lateral_endpoint_mean"
    )
    rail = select_visual_landmark(profile, "rail_lateral_endpoint_mean")
    assert rail["required_point_ids"] == [
        "rail_lateral_left",
        "rail_lateral_right",
    ]
    assert "neon-green" in rail["description_for_vlm"]
    assert "TOOL_CENTER_POINT" in profile["action_frames"][0]["semantic_roles"]
    assert profile["robot_compatibility"]["controlled_frame"] == (
        "rebot_arm_tool"
    )
    assert profile["robot_compatibility"]["arm_base_frame"] == (
        "rebot_arm_base"
    )
    assert profile["kinematic_attachment"][
        "terminal_joint_to_controlled_frame"
    ]["translation_m"] == [0.0, 0.0, 0.15539]
    assert profile["landmark_fallback_policy"]["selection_order"] == [
        "rail_lateral_endpoint_mean",
        "gripper_tip_pair_mean",
    ]
    assert not profile["landmark_fallback_policy"][
        "automatic_substitution_allowed"
    ]
    assert rail["aggregation_policy"] == {
        "method": "ARITHMETIC_MEAN_OF_ALL_REGISTERED_3D_POINTS",
        "requires_all_points": True,
        "missing_point_policy": "REJECT_OBSERVATION",
    }
    assert profile["refinement_policy"] == {
        "second_vlm_review_raw_delta_threshold_m": 0.005,
        "maximum_raw_translation_delta_m": 0.1,
        "maximum_adopted_translation_delta_m": 0.025,
        "minimum_landmark_confidence": 0.75,
        "minimum_same_surface_confidence": 0.75,
    }


def test_blade_profile_contains_user_trial_handle_landmark() -> None:
    profile = load_effector_profile(BLADE_PROFILE_PATH)
    landmark = select_visual_landmark(profile)

    assert profile["profile_revision"] == "rebot-b601-dm-5-inch-blade-v2"
    assert landmark["required_point_ids"] == [
        "knife_handle_blade_junction",
        "knife_handle_rear_endpoint",
    ]
    description = landmark["description_for_vlm"]
    assert "military-green handle" in description
    assert "do not classify it as a knife" in description
    assert "Reject the scene only when" in description
    assert np.allclose(
        resolve_tool_landmark_point(landmark),
        [-0.09, 0.01, -0.07],
    )
    assert landmark["tool_point_binding"][
        "landmark_to_controlled_frame_translation_m"
    ] == [0.09, -0.01, 0.07]


@pytest.mark.parametrize(
    "landmark_id",
    [None, "", "default", "DEFAULT", "profile_default", "profile-default", "auto"],
)
def test_default_landmark_sentinels_select_profile_default(
    landmark_id: str | None,
) -> None:
    profile = load_effector_profile(PROFILE_PATH)

    selected = select_visual_landmark(profile, landmark_id)

    assert selected["landmark_id"] == "rail_lateral_endpoint_mean"


def test_real_landmark_named_default_takes_precedence_over_sentinel() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    explicit_default = json.loads(
        json.dumps(profile["visual_alignment_landmarks"][0])
    )
    explicit_default["landmark_id"] = "default"
    profile["visual_alignment_landmarks"].append(explicit_default)

    selected = select_visual_landmark(profile, "default")

    assert selected["landmark_id"] == "default"


def test_unknown_landmark_is_still_rejected() -> None:
    profile = load_effector_profile(PROFILE_PATH)

    with pytest.raises(ValueError, match="not in the effector profile"):
        select_visual_landmark(profile, "unknown_landmark")


def test_profile_schema_file_is_valid_json() -> None:
    schema = json.loads(
        (SKILL_ROOT / "schemas" / "effector_alignment_profile.v1.schema.json")
        .read_text(encoding="utf-8")
    )

    assert schema["properties"]["schema"]["const"] == (
        "midbrain.effector_alignment_profile"
    )
    validate(instance=load_effector_profile(PROFILE_PATH), schema=schema)


def test_public_mounted_effector_schema_validates_known_profiles() -> None:
    schema = json.loads(
        (
            WORKSPACE_ROOT
            / "contracts"
            / "schemas"
            / "mounted_effector_profile.v1.schema.json"
        ).read_text(encoding="utf-8")
    )

    for path in (PROFILE_PATH, BLADE_PROFILE_PATH):
        validate(
            instance=json.loads(path.read_text(encoding="utf-8")),
            schema=schema,
        )


def test_mounted_effector_schema_allows_namespaced_unknown_extensions() -> None:
    schema = json.loads(
        (
            WORKSPACE_ROOT
            / "contracts"
            / "schemas"
            / "mounted_effector_profile.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile["extensions"]["example.consumer.experimental.v1"] = {
        "consumer_owned_setting": True
    }

    validate(instance=profile, schema=schema)


def test_known_alignment_extension_remains_strict() -> None:
    schema = json.loads(
        (
            WORKSPACE_ROOT
            / "contracts"
            / "schemas"
            / "mounted_effector_profile.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    landmark = profile["extensions"][
        "midbrain.skill.refine_arm_root_translation.v1"
    ]["visual_alignment_landmarks"][0]
    del landmark["aggregation_policy"]["requires_all_points"]

    with pytest.raises(ValidationError):
        validate(instance=profile, schema=schema)


def test_every_machine_schema_has_a_unique_identifier() -> None:
    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((SKILL_ROOT / "schemas").glob("*.json"))
    ]
    identifiers = [schema["$id"] for schema in schemas]

    assert len(schemas) == 6
    assert len(set(identifiers)) == len(identifiers)


def test_measured_rail_center_to_tip_offset_has_explicit_inverse() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    rail = select_visual_landmark(profile)
    tip = select_visual_landmark(profile, "gripper_tip_pair_mean")

    assert np.array_equal(resolve_tool_landmark_point(tip), np.zeros(3))
    assert np.allclose(resolve_tool_landmark_point(rail), [-0.08, 0.0, 0.0])
    assert rail["tool_point_binding"][
        "landmark_to_controlled_frame_translation_m"
    ] == [0.08, 0.0, 0.0]
    assert rail["tool_point_binding"][
        "controlled_frame_to_landmark_translation_m"
    ] == [-0.08, 0.0, 0.0]
    assert (
        "controlled-frame +X"
        in rail["tool_point_binding"]["measurement_note"]
    )


def test_inconsistent_bidirectional_binding_is_rejected() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    rail = select_visual_landmark(profile)
    rail["tool_point_binding"][
        "landmark_to_controlled_frame_translation_m"
    ] = [0.07, 0.0, 0.0]

    with pytest.raises(RuntimeError, match="is not the inverse"):
        resolve_tool_landmark_point(rail)


def test_rail_center_offset_follows_rotated_controlled_x_axis() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    rail = select_visual_landmark(profile)
    tool_to_rail = resolve_tool_landmark_point(rail)
    rail_to_tip = np.asarray(
        rail["tool_point_binding"][
            "landmark_to_controlled_frame_translation_m"
        ],
        dtype=np.float64,
    )
    base_from_tool = np.eye(4, dtype=np.float64)
    base_from_tool[:3, :3] = np.asarray(
        [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    base_from_tool[:3, 3] = [0.4, 0.2, 0.3]
    active_world_from_base = np.eye(4, dtype=np.float64)
    active_world_from_base[:3, 3] = [1.0, 2.0, 3.0]
    base_rail = (
        base_from_tool[:3, :3] @ tool_to_rail
        + base_from_tool[:3, 3]
    )
    observed_world_rail = (
        active_world_from_base[:3, :3] @ base_rail
        + active_world_from_base[:3, 3]
    )

    result = prepare_translation_refinement(
        active_world_from_base=active_world_from_base,
        base_from_tool=base_from_tool,
        tool_landmark_point_m=tool_to_rail,
        observed_world_landmark_point_m=observed_world_rail,
        adoption_factor=1.0,
        review_threshold_m=0.005,
        source_revision=1,
        identities={"test": "identity"},
        landmark_id=rail["landmark_id"],
        observation_provenance={},
    )
    reconstructed_world_tip = observed_world_rail + (
        active_world_from_base[:3, :3]
        @ base_from_tool[:3, :3]
        @ rail_to_tip
    )
    expected_world_tip = (
        active_world_from_base[:3, :3] @ base_from_tool[:3, 3]
        + active_world_from_base[:3, 3]
    )

    assert np.allclose(result["raw_translation_delta_m"], np.zeros(3))
    assert np.allclose(
        result["estimated_world_controlled_frame_origin_m"],
        expected_world_tip,
    )
    assert result["controlled_frame_to_landmark_translation_m"] == [
        -0.08,
        0.0,
        0.0,
    ]
    assert result["landmark_to_controlled_frame_translation_m"] == [
        0.08,
        0.0,
        0.0,
    ]
    assert np.allclose(reconstructed_world_tip, expected_world_tip)


def test_detection_preserves_separate_per_image_coordinates() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    normalized = validate_landmark_detection(
        detection_for(landmark),
        landmark=landmark,
        rgb_grid=(10, 10),
        registered_depth_grid=(11, 11),
    )

    assert normalized["points"][0]["rgb_yx_0_1000"] == [444, 333]
    assert normalized["points"][0]["registered_depth_yx_0_1000"] == [500, 400]


def test_canonical_coordinate_converts_independently_to_image_pixels() -> None:
    assert canonical_yx_to_pixel(
        [310, 493],
        grid=(1080, 1920),
        name="logged_point",
    ) == [334, 946]
    assert canonical_yx_to_pixel(
        [0, 1000],
        grid=(576, 640),
        name="image_edges",
    ) == [0, 639]


def test_logged_canonical_coordinate_renders_at_converted_pixel() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    detection = detection_for(landmark)
    detection["points"][0]["rgb_yx_0_1000"] = [310, 493]
    detection["points"][0]["registered_depth_yx_0_1000"] = [310, 493]

    annotations = build_detection_annotations(
        detection=detection,
        rgb_grid=(1080, 1920),
        registered_depth_grid=(1080, 1920),
    )

    assert annotations[0]["x"] == pytest.approx(946.5 / 1920.0)
    assert annotations[0]["y"] == pytest.approx(334.5 / 1080.0)
    assert annotations[1]["x"] == pytest.approx(946.5 / 1920.0)
    assert annotations[1]["y"] == pytest.approx(334.5 / 1080.0)


def test_landmark_midpoint_is_mean_of_registered_3d_points() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    detection = detection_for(landmark)
    depth = np.full((11, 11), np.nan)
    depth[5, 4] = 1.0
    depth[5, 6] = 3.0

    resolved = resolve_profile_landmark(
        detection=detection,
        landmark=landmark,
        rgb_grid=(10, 10),
        registered_depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
        world_from_camera=np.eye(4),
    )

    first = np.asarray(resolved["registered_points"][0]["camera_system_point_m"])
    second = np.asarray(resolved["registered_points"][1]["camera_system_point_m"])
    assert np.allclose(
        resolved["camera_system_landmark_point_m"],
        0.5 * (first + second),
    )
    assert resolved["registered_depth_landmark_pixel_yx"] == [5, 6]
    assert resolved["registered_depth_landmark_pixel_yx"] != [5, 5]


def test_three_point_landmark_requires_all_points_and_uses_3d_mean() -> None:
    landmark = {
        "landmark_id": "three_point_test",
        "geometry": "MEAN_OF_REGISTERED_3D_POINTS",
        "required_point_ids": ["point_a", "point_b", "point_c"],
    }
    detection = {
        "schema": "midbrain.effector_landmark_detection",
        "schema_version": 2,
        "scene_suitable": True,
        "landmark_id": "three_point_test",
        "coordinate_space": "NORMALIZED_YX_0_1000_PER_IMAGE",
        "reason": "All three configured features are visible.",
        "points": [
            {
                "point_id": point_id,
                "rgb_yx_0_1000": [500, x],
                "registered_depth_yx_0_1000": [500, x],
                "confidence": 0.95,
                "same_surface_confidence": 0.95,
                "reason": "Configured feature and same-surface depth are visible.",
            }
            for point_id, x in zip(
                landmark["required_point_ids"],
                [300, 500, 700],
                strict=True,
            )
        ],
    }
    depth = np.ones((11, 11), dtype=np.float64)

    resolved = resolve_profile_landmark(
        detection=detection,
        landmark=landmark,
        rgb_grid=(11, 11),
        registered_depth_m=depth,
        intrinsics={"fx": 10.0, "fy": 10.0, "cx": 5.0, "cy": 5.0},
        world_from_camera=np.eye(4),
    )
    points = np.asarray(
        [item["camera_system_point_m"] for item in resolved["registered_points"]]
    )
    assert np.allclose(
        resolved["camera_system_landmark_point_m"],
        np.mean(points, axis=0),
    )

    missing = json.loads(json.dumps(detection))
    missing["points"].pop()
    with pytest.raises(RuntimeError, match="did not return every required point"):
        validate_landmark_detection(
            missing,
            landmark=landmark,
            rgb_grid=(11, 11),
            registered_depth_grid=(11, 11),
        )


def test_exact_vlm_selected_depth_is_required_without_neighbor_snap() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    depth = np.full((11, 11), np.nan)
    depth[5, 5] = 1.0
    depth[5, 6] = 1.0

    with pytest.raises(
        InvalidDepthSelectionError,
        match="pixels without valid exact depth",
    ) as captured:
        resolve_profile_landmark(
            detection=detection_for(landmark),
            landmark=landmark,
            rgb_grid=(10, 10),
            registered_depth_m=depth,
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
            world_from_camera=np.eye(4),
        )

    assert captured.value.invalid_points == [
        {
            "point_id": "rail_lateral_left",
            "canonical_coordinate_yx_0_1000": [500, 400],
            "converted_pixel_yx": [5, 4],
            "observed_depth_m": None,
        }
    ]


def test_low_confidence_rejects_translation_eligibility() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    depth = np.ones((11, 11), dtype=np.float64)

    resolved = resolve_profile_landmark(
        detection=detection_for(landmark, confidence=0.4),
        landmark=landmark,
        rgb_grid=(10, 10),
        registered_depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
        world_from_camera=np.eye(4),
    )

    assert resolved["status"] == "REJECTED_OBSERVATION"
    assert not resolved["eligible_for_translation_refinement"]
    assert "confidence" in resolved["quality_reasons"][0]


def test_prompt_requires_semantic_rgb_to_depth_correspondence() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile, "rail_lateral_endpoint_mean")

    prompt = build_landmark_prompt(
        profile=profile,
        landmark=landmark,
        rgb_grid=(720, 1280),
        registered_depth_grid=(576, 640),
    )

    assert "independently select a valid depth pixel" in prompt
    assert "every selected depth coordinate must land on WHITE" in prompt
    assert "display-name metadata are not visual classification requirements" in prompt
    assert "Do not choose the same numeric pixel by default" in prompt
    assert "reflection" in prompt
    assert "exactly these seven top-level keys and no others" in prompt
    assert "midbrain.effector_landmark_detection" in prompt
    assert "landmark_id exactly to rail_lateral_endpoint_mean" in prompt
    assert "exactly these six keys and no others" in prompt
    assert "rail_lateral_left, rail_lateral_right" in prompt
    assert "two-integer JSON array in [y, x] order" in prompt
    assert "NORMALIZED_YX_0_1000_PER_IMAGE" in prompt
    assert "Do not return literal source-image pixels" in prompt
    assert "Do not wrap the JSON in Markdown" in prompt


def test_schema_mismatch_reports_missing_and_unexpected_fields() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    malformed = detection_for(landmark)
    malformed.pop("schema_version")
    malformed["extra"] = True

    with pytest.raises(RuntimeError) as captured:
        validate_landmark_detection(
            malformed,
            landmark=landmark,
            rgb_grid=(10, 10),
            registered_depth_grid=(11, 11),
        )

    message = str(captured.value)
    assert "missing: schema_version" in message
    assert "unexpected: extra" in message


def test_visual_annotations_and_marked_overlap_use_existing_contract_shape() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    detection = detection_for(landmark)
    depth = np.ones((11, 11), dtype=np.float64)
    resolved = resolve_profile_landmark(
        detection=detection,
        landmark=landmark,
        rgb_grid=(10, 10),
        registered_depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
        world_from_camera=np.eye(4),
    )

    annotations = build_visual_annotations(
        detection=detection,
        resolved_landmark=resolved,
        rgb_grid=(10, 10),
        registered_depth_grid=(11, 11),
    )
    png = render_marked_overlap_png(
        np.zeros((11, 11, 3), dtype=np.uint8),
        detection=detection,
        resolved_landmark=resolved,
    )

    assert len(annotations) == 5
    assert all(annotation["type"] == "point" for annotation in annotations)
    assert annotations[0]["applies_to_channels"] == ["rgb"]
    assert annotations[1]["applies_to_channels"] == [
        "depth",
        "depth_validity",
        "rgb_depth",
        "marked_overlap",
    ]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    crop, panels, crop_grid = render_landmark_review_crop_png(
        png,
        detection=detection,
        registered_depth_grid=(11, 11),
    )
    crop_annotations = build_landmark_review_crop_annotations(
        crop_panels=panels,
        crop_grid=crop_grid,
    )
    assert crop.startswith(b"\x89PNG\r\n\x1a\n")
    assert crop_grid == [640, 1280]
    assert all(
        panel["source_bounds_yxyx"] == [0, 0, 11, 11]
        for panel in panels
    )
    assert len(crop_annotations) == 2
    assert all(
        annotation["applies_to_channels"] == ["landmark_review_crop"]
        for annotation in crop_annotations
    )
    assert all(0.0 <= annotation["x"] <= 1.0 for annotation in crop_annotations)
    assert all(0.0 <= annotation["y"] <= 1.0 for annotation in crop_annotations)


def test_review_crop_keeps_each_far_apart_point_magnified() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    detection = detection_for(landmark)
    detection["points"][0]["registered_depth_yx_0_1000"] = [0, 0]
    detection["points"][1]["registered_depth_yx_0_1000"] = [1000, 1000]
    full = np.zeros((1080, 1920, 3), dtype=np.uint8)
    marked = render_marked_overlap_png(
        full,
        detection=detection,
        resolved_landmark={},
    )

    _, panels, crop_grid = render_landmark_review_crop_png(
        marked,
        detection=detection,
        registered_depth_grid=(1080, 1920),
    )

    assert crop_grid == [640, 1280]
    assert len(panels) == 2
    assert all(
        panel["source_bounds_yxyx"] != [0, 0, 1080, 1920]
        for panel in panels
    )


def test_alignment_back_projections_add_old_and_proposed_svg_points() -> None:
    profile = load_effector_profile(PROFILE_PATH)
    landmark = select_visual_landmark(profile)
    detection = detection_for(landmark)
    depth = np.ones((11, 11), dtype=np.float64)
    resolved = resolve_profile_landmark(
        detection=detection,
        landmark=landmark,
        rgb_grid=(11, 11),
        registered_depth_m=depth,
        intrinsics={"fx": 10.0, "fy": 10.0, "cx": 5.0, "cy": 5.0},
        world_from_camera=np.eye(4),
    )
    source = np.eye(4)
    source[2, 3] = 1.0
    proposed = source.copy()
    proposed[0, 3] = 0.1
    fk = np.eye(4)
    fk[2, 3] = 0.2
    projections = build_alignment_image_projections(
        source_world_from_base=source,
        proposed_world_from_base=proposed,
        base_from_tool=fk,
        tool_landmark_point_m=[0.0, 0.0, 0.0],
        world_from_camera=np.eye(4),
        intrinsics={"fx": 10.0, "fy": 10.0, "cx": 5.0, "cy": 5.0},
        registered_depth_grid=(11, 11),
    )
    annotations = build_visual_annotations(
        detection=detection,
        resolved_landmark=resolved,
        rgb_grid=(11, 11),
        registered_depth_grid=(11, 11),
        alignment_projections=projections,
    )

    assert [point["annotation_id"] for point in projections] == [
        "old-arm-base-origin",
        "new-arm-base-origin",
        "old-alignment-landmark",
        "new-alignment-landmark",
    ]
    assert all(point["in_image"] for point in projections)
    assert {annotation["id"] for annotation in annotations} >= {
        point["annotation_id"] for point in projections
    }
