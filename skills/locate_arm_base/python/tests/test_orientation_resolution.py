from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from locate_arm_base.arm_profile import ArmProfileStore
from locate_arm_base.orientation import (
    EffectorLandmarkSpec,
    EffectorPointObservation,
    resolve_orientation_from_effector_fk,
    resolve_orientation_from_world_x_hint,
)


ROOT = Path(__file__).resolve().parents[4]


def _profile():
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    return ArmProfileStore(ROOT, config).load().model_profile


def test_effector_fk_projection_recovers_every_profiled_yaw_candidate() -> None:
    profile = _profile()
    camera_from_mesh = np.eye(4, dtype=np.float64)
    camera_from_mesh[:3, 3] = [0.02, -0.03, 2.0]
    base_from_tool = np.eye(4, dtype=np.float64)
    base_from_tool[:3, 3] = [0.36, 0.11, 0.19]
    landmark = EffectorLandmarkSpec(
        landmark_id="test_effector_point",
        display_name="test effector",
        point_ids=("visible_point",),
        description_for_vlm="test",
        controlled_frame_id="test_tool",
        arm_base_frame="rebot_arm_base",
        controlled_frame_to_landmark_translation_m=(0.0, 0.0, 0.0),
        source="TEST",
    )
    intrinsics = {"fx": 220.0, "fy": 220.0, "cx": 320.0, "cy": 240.0}
    image_size = (640, 480)
    tool_origin = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)

    for expected in profile.candidates:
        camera_from_base = (
            camera_from_mesh
            @ expected.matrix
            @ profile.centered_mesh_from_arm_base
        )
        camera_point = (camera_from_base @ base_from_tool @ tool_origin)[:3]
        pixel_x = intrinsics["fx"] * camera_point[0] / camera_point[2] + intrinsics["cx"]
        pixel_y = intrinsics["fy"] * camera_point[1] / camera_point[2] + intrinsics["cy"]
        normalized_y = round(1000.0 * pixel_y / float(image_size[1] - 1))
        normalized_x = round(1000.0 * pixel_x / float(image_size[0] - 1))
        observation = EffectorPointObservation(
            identified=True,
            points_yx_0_1000=(("visible_point", normalized_y, normalized_x),),
            confidence=0.1,
            rationale="Rough point only.",
            model="test",
            response_id="test-response",
        )

        resolved = resolve_orientation_from_effector_fk(
            observation=observation,
            camera_from_centered_mesh=camera_from_mesh,
            upright_correction=np.eye(4, dtype=np.float64),
            profile=profile,
            base_from_controlled_frame=base_from_tool,
            landmark_spec=landmark,
            camera_intrinsics=intrinsics,
            image_size=image_size,
        )

        assert resolved.candidate_id == expected.candidate_id
        assert resolved.decision_basis == (
            "SINGLE_VLM_EFFECTOR_POINT_WITH_TIMESTAMPED_FK"
        )


def test_exact_world_x_hint_recovers_every_profiled_yaw_candidate() -> None:
    profile = _profile()
    camera_from_mesh = np.eye(4, dtype=np.float64)
    camera_from_mesh[:3, 3] = [0.02, -0.03, 2.0]
    world_from_camera = np.eye(4, dtype=np.float64)

    for expected in profile.candidates:
        world_from_base = (
            world_from_camera
            @ camera_from_mesh
            @ expected.matrix
            @ profile.centered_mesh_from_arm_base
        )
        expected_positive_x_world = world_from_base[:3, 0]

        resolved = resolve_orientation_from_world_x_hint(
            rough_positive_x_world=expected_positive_x_world.tolist(),
            world_from_camera=world_from_camera,
            camera_from_centered_mesh=camera_from_mesh,
            upright_correction=np.eye(4, dtype=np.float64),
            profile=profile,
        )

        assert resolved.candidate_id == expected.candidate_id
        assert resolved.decision_basis == "AGENT_SUPPLIED_ROUGH_WORLD_POSITIVE_X"
