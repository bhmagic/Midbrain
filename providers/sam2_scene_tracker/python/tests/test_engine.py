from __future__ import annotations

from dataclasses import replace
import time

import numpy as np

from sam2_scene_tracker.engine import Sam2SceneTrackerEngine
from sam2_scene_tracker.fusion import PersistentSemanticVoxelMap
from sam2_scene_tracker.prompts import ARM_OBJECT_ID, VisualPrompt
from sam2_scene_tracker.rgbd import RgbdFrame


def _prompt(object_id: str) -> VisualPrompt:
    return VisualPrompt(
        object_id=object_id,
        region_id="test",
        box_yxyx=(0, 0, 1000, 1000),
        positive_points_yx=((250, 250), (750, 750)),
        confidence=1.0,
    )


class _Fabric:
    def __init__(
        self,
        *,
        with_policy: bool = True,
        include_work_object: bool = False,
    ) -> None:
        self.published = []
        objects = [
            {
                "object_id": "table",
                "type": "KEEP_OUT",
                "description": "the table",
            }
        ]
        if include_work_object:
            objects.append(
                {
                    "object_id": "roll",
                    "type": "WORK_OBJECT",
                    "description": "the toilet paper roll",
                }
            )
        self.policy = (
            {
                "contract_version": 1,
                "policy_id": "test",
                "objects": objects,
                "arm_description": "the complete robot arm and gripper",
            }
            if with_policy
            else None
        )

    def latest_optional(self, stream):
        return self.policy if stream == "policy" else None

    def transform(self, *, from_frame, **_arguments):
        if from_frame == "rebot_arm_tool":
            translation = [0.2, 0.0, 0.2]
        else:
            translation = [0.0, 0.0, 0.0]
        return {
            "translation_m": translation,
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        }

    def publish(self, observation):
        self.published.append(observation)
        return {"accepted": True}


class _NoCameraTransformFabric(_Fabric):
    def transform(self, *, from_frame, **arguments):
        if from_frame != "rebot_arm_tool":
            raise RuntimeError("camera-to-arm transform unavailable")
        return super().transform(from_frame=from_frame, **arguments)


class _Capture:
    def __init__(self) -> None:
        self.frame = RgbdFrame(
            rgb=np.zeros((6, 8, 3), dtype=np.uint8),
            depth_m=np.ones((6, 8), dtype=np.float32) * 0.4,
            intrinsics={"fx": 10.0, "fy": 10.0, "cx": 3.5, "cy": 2.5},
            observed_at_us=time.time_ns() // 1000,
            frame_number=1,
            camera_frame="camera",
            session_epoch="epoch-1",
            calibration_revision="cal-1",
            camera_provider_id="camera.test",
            camera_provider_instance_id="instance-1",
            camera_boot_id="boot-1",
            source_observations={},
        )

    def capture(self, **_arguments):
        result = self.frame
        self.frame = replace(
            result,
            observed_at_us=result.observed_at_us + 1_000_000,
            frame_number=result.frame_number + 1,
        )
        return result


class _Annotator:
    def annotate(self, _image, policy):
        return {
            ARM_OBJECT_ID: [_prompt(ARM_OBJECT_ID)],
            **{
                value.object_id: [_prompt(value.object_id)]
                for value in policy.objects
            },
        }

    def close(self):
        pass

    def validate_masks(self, _image, _depth, masks, policy):
        return {
            "accepted": set(masks)
            == {ARM_OBJECT_ID, *(value.object_id for value in policy.objects)},
            "object_results": [],
            "model_id": "test-vlm",
        }


class _Tracker:
    def __init__(self) -> None:
        self.shape = None

    def set_image(self, image):
        self.shape = image.shape[:2]

    def segment(self, prompts):
        mask = np.zeros(self.shape, dtype=bool)
        if prompts[0].object_id == ARM_OBJECT_ID:
            mask[:, 3:5] = True
        elif prompts[0].object_id == "roll":
            mask[:3, :3] = True
        else:
            mask[3:, :] = True
        return mask, 0.95

    def close(self):
        pass


class _RejectingAnnotator(_Annotator):
    def __init__(self):
        self.annotation_calls = 0
        self.review_calls = 0

    def annotate(self, image, policy):
        self.annotation_calls += 1
        return super().annotate(image, policy)

    def validate_masks(self, _image, _depth, _masks, _policy):
        self.review_calls += 1
        return {
            "accepted": False,
            "object_results": [
                {
                    "object_id": "table",
                    "acceptable": False,
                    "problem": "TARGET_SPILL",
                }
            ],
            "model_id": "test-vlm",
        }


def _config():
    return {
        "policy_stream": "policy",
        "vlm_refresh_interval_s": 7.5,
        "tracking_interval_s": 1.0,
        "arm_mask_dilation_pixels": 0,
        "depth_pixel_stride": 1,
        "minimum_depth_m": 0.05,
        "maximum_depth_m": 5.0,
        "assertion_freshness_ms": 3000,
        "publish_pushable_geometry": False,
    }


def test_engine_requires_an_explicit_fabric_policy() -> None:
    engine = Sam2SceneTrackerEngine(
        fabric=_Fabric(with_policy=False),  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=_Annotator(),
        tracker=_Tracker(),
        semantic_map=PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02),
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        try:
            engine.tick()
        except RuntimeError as error:
            assert "USER_OBSTACLE_DESCRIPTION_REQUIRED" in str(error)
        else:
            raise AssertionError("engine accepted a missing Fabric policy")
    finally:
        engine.close()


def test_engine_waits_for_vlm_then_publishes_only_declared_obstacle() -> None:
    fabric = _Fabric()
    engine = Sam2SceneTrackerEngine(
        fabric=fabric,  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=_Annotator(),
        tracker=_Tracker(),
        semantic_map=PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02),
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        assert engine.tick() is None
        assert engine.latest_rgb_png is not None
        assert engine.latest_depth_png is not None
        assert engine.annotation_future is not None
        engine.annotation_future.result(timeout=1.0)

        observation = engine.tick()

        assert observation is not None
        assert observation["valid"]
        assertions = observation["data"]["assertions"]
        assert assertions
        assert {value["object_id"] for value in assertions} == {"table"}
        assert {value["type"] for value in assertions} == {"KEEP_OUT"}
        assert {value["roi_scope"] for value in assertions} == {
            "HAND_ANGULAR_4PI"
        }
        projection = observation["data"]["angular_projection"]
        assert projection["profile_id"] == (
            "SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1"
        )
        assert projection["direction_count"] == 4096
        assert projection["occupied_direction_count"] == len(assertions)
        assert projection["observed_at_us"] == (
            engine.last_diagnostics["source_observed_at_us"]
        )
        aabbs = observation["data"]["visible_surface_aabbs"]
        assert aabbs == []
        assert engine.latest_visualization_png is not None
        assert engine.last_diagnostics["unclaimed_visible_policy"] == (
            "PUSHABLE_IGNORED"
        )
        erosion = engine.last_diagnostics["mask_erosion"]["table"]
        assert erosion["type"] == "KEEP_OUT"
        assert erosion["erosion_m"] == 0.02
        assert erosion["status"] == "APPLIED"
    finally:
        engine.close()


def test_engine_publishes_aabb_only_for_work_object() -> None:
    fabric = _Fabric(include_work_object=True)
    engine = Sam2SceneTrackerEngine(
        fabric=fabric,  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=_Annotator(),
        tracker=_Tracker(),
        semantic_map=PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02),
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        assert engine.tick() is None
        assert engine.annotation_future is not None
        engine.annotation_future.result(timeout=1.0)

        observation = engine.tick()

        assert observation is not None
        aabbs = observation["data"]["visible_surface_aabbs"]
        assert len(aabbs) == 1
        assert aabbs[0]["object_id"] == "roll"
        assert aabbs[0]["type"] == "WORK_OBJECT"
        assert aabbs[0]["freshness_ms"] == 5000
        erosion = engine.last_diagnostics["mask_erosion"]["roll"]
        assert erosion["erosion_m"] == 0.01
        assert erosion["output_mask_pixels"] <= erosion["input_mask_pixels"]
    finally:
        engine.close()


def test_engine_keeps_rgb_depth_and_masks_live_without_arm_transform() -> None:
    engine = Sam2SceneTrackerEngine(
        fabric=_NoCameraTransformFabric(),  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=_Annotator(),
        tracker=_Tracker(),
        semantic_map=PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02),
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        engine.tick()
        assert engine.annotation_future is not None
        engine.annotation_future.result(timeout=1.0)

        observation = engine.tick()
        assert observation is not None
        assert observation["valid"] is False
        assert observation["data"]["mapping_failure"]["status"] == (
            "CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE_2D_TRACKING_ACTIVE"
        )
        assert observation["data"]["coverage"]["attempt_count"] == 0
        assert engine.latest_rgb_png is not None
        assert engine.latest_depth_png is not None
        assert engine.latest_visualization_png is not None
        assert engine.last_diagnostics["status"] == (
            "CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE_2D_TRACKING_ACTIVE"
        )
        assert engine.last_diagnostics["blocking_prerequisite"] == {
            "status": "TRANSFORM_UNAVAILABLE",
            "requires_external_action": True,
            "from_frame": "camera",
            "to_frame": "rebot_arm_base",
            "message": (
                "Establish or restore the current camera-to-arm-base "
                "calibration before requesting 3D semantic geometry."
            ),
        }
    finally:
        engine.close()


def test_repeated_engine_frames_do_not_duplicate_persistent_voxels() -> None:
    fabric = _Fabric()
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    engine = Sam2SceneTrackerEngine(
        fabric=fabric,  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=_Annotator(),
        tracker=_Tracker(),
        semantic_map=semantic_map,
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        engine.tick()
        assert engine.annotation_future is not None
        engine.annotation_future.result(timeout=1.0)
        engine.tick()
        first_count = semantic_map.snapshot()["objects"]["table"][
            "persistent_voxel_count"
        ]
        engine.tick()
        second_count = semantic_map.snapshot()["objects"]["table"][
            "persistent_voxel_count"
        ]

        assert second_count == first_count
    finally:
        engine.close()


def test_new_policy_fuses_no_spheres_after_three_vlm_quality_rejections() -> None:
    annotator = _RejectingAnnotator()
    semantic_map = PersistentSemanticVoxelMap(fusion_voxel_edge_m=0.02)
    engine = Sam2SceneTrackerEngine(
        fabric=_Fabric(),  # type: ignore[arg-type]
        capture=_Capture(),  # type: ignore[arg-type]
        annotator=annotator,
        tracker=_Tracker(),
        semantic_map=semantic_map,
        config=_config(),
        provider_id="tracker",
        provider_instance_id="instance",
        boot_id="boot",
    )
    try:
        assert engine.tick() is None
        assert engine.annotation_future is not None
        engine.annotation_future.result(timeout=1.0)

        observation = engine.tick()
        assert annotator.annotation_calls == 3
        assert annotator.review_calls == 3
        assert semantic_map.snapshot()["objects"] == {}
        assert observation is not None
        assert observation["valid"] is False
        assert observation["data"]["assertions"] == []
        assert observation["data"]["coverage"]["attempt_count"] == 3
        assert engine.last_diagnostics["status"] == (
            "VLM_MASK_QUALITY_REJECTED_AFTER_3_ATTEMPTS"
        )
    finally:
        engine.close()
