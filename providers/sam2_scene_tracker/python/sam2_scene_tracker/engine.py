from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import time
from typing import Any, Protocol

import cv2
import numpy as np

from .clients import FabricClient
from .angular_geometry import (
    ANGULAR_PROFILE_ID,
    build_hand_angular_assertions,
    build_visible_surface_aabb,
    hand_angular_projection_metadata,
)
from .fusion import PersistentSemanticVoxelMap
from .policy import SceneSegmentationPolicy, parse_policy
from .prompts import ARM_OBJECT_ID, VisualPrompt
from .rgbd import RgbdCapture, RgbdFrame
from .sam_backend import Sam2ImageTracker, prompt_from_mask
from .segmentation import (
    constrain_mask_to_prompted_depth_component,
    erode_mask_by_metric_boundary,
    partition_semantic_masks,
    project_masked_depth_to_frame,
)


class AnnotatorProtocol(Protocol):
    def annotate(
        self,
        image_rgb: np.ndarray,
        policy: SceneSegmentationPolicy,
    ) -> dict[str, list[VisualPrompt]]: ...

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...

class TrackerProtocol(Protocol):
    def set_image(self, image_rgb: np.ndarray) -> None: ...

    def segment(self, prompts: list[VisualPrompt]) -> tuple[np.ndarray, float]: ...

    def close(self) -> None: ...


class Sam2SceneTrackerEngine:
    def __init__(
        self,
        *,
        fabric: FabricClient,
        capture: RgbdCapture,
        annotator: AnnotatorProtocol,
        tracker: TrackerProtocol,
        semantic_map: PersistentSemanticVoxelMap,
        config: dict[str, Any],
        provider_id: str,
        provider_instance_id: str,
        boot_id: str,
    ) -> None:
        self.fabric = fabric
        self.capture = capture
        self.annotator = annotator
        self.tracker = tracker
        self.semantic_map = semantic_map
        self.config = config
        self.provider_id = provider_id
        self.provider_instance_id = provider_instance_id
        self.boot_id = boot_id
        self.sequence = 0
        self.policy: SceneSegmentationPolicy | None = None
        self.prompts: dict[str, list[VisualPrompt]] = {}
        self.previous_masks: dict[str, np.ndarray] = {}
        self.annotation_future: Future[Any] | None = None
        self.annotation_policy_identity: str | None = None
        self.annotation_started_monotonic: float | None = None
        self.last_annotation_completed_monotonic: float | None = None
        self.annotation_error: str | None = None
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sam2-scene-vlm",
        )
        self.last_observation: dict[str, Any] | None = None
        self.last_diagnostics: dict[str, Any] = {}
        self.latest_rgb_png: bytes | None = None
        self.latest_depth_png: bytes | None = None
        self.latest_visualization_png: bytes | None = None
        self.arm_motion_active = False
        self.visual_motion_active = False
        self.visual_motion_score = 0.0
        self.last_visual_motion_monotonic: float | None = None
        self.previous_motion_thumbnail: np.ndarray | None = None
        self.last_min_sam2_score: float | None = None
        self.last_quality_review: dict[str, Any] | None = None
        self.latest_angular_assertions: list[dict[str, Any]] = []
        self.latest_angular_projection: dict[str, Any] | None = None
        self.latest_visible_surface_aabbs: list[dict[str, Any]] = []

    def set_arm_motion_active(self, active: bool) -> None:
        self.arm_motion_active = bool(active)

    def _update_visual_motion(self, frame: RgbdFrame, now: float) -> None:
        gray = cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2GRAY)
        thumbnail = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        previous = self.previous_motion_thumbnail
        self.previous_motion_thumbnail = thumbnail
        if previous is None:
            self.visual_motion_score = 0.0
        else:
            difference = cv2.absdiff(thumbnail, previous)
            self.visual_motion_score = float(np.mean(difference)) / 255.0
            threshold = float(self.config.get("visual_motion_threshold", 0.015))
            if self.visual_motion_score >= threshold:
                self.last_visual_motion_monotonic = now
        hold_s = float(self.config.get("visual_motion_hold_s", 2.0))
        last = self.last_visual_motion_monotonic
        self.visual_motion_active = last is not None and now - last <= hold_s

    @property
    def scene_motion_active(self) -> bool:
        return self.arm_motion_active or self.visual_motion_active

    def _vlm_refresh_interval(self) -> float:
        low_confidence = (
            self.last_min_sam2_score is not None
            and self.last_min_sam2_score
            < float(self.config.get("sam2_low_confidence_threshold", 0.60))
        )
        if self.scene_motion_active or low_confidence:
            return float(self.config.get("vlm_motion_refresh_interval_s", 20.0))
        return float(self.config.get("vlm_stationary_refresh_interval_s", 40.0))

    def _annotator_status(self) -> dict[str, Any]:
        describe = getattr(self.annotator, "describe", None)
        return describe() if callable(describe) else {}

    def _load_policy(self) -> SceneSegmentationPolicy:
        stream = str(
            self.config.get("policy_stream")
            or "robot_arm.scene.segmentation_policy"
        )
        observation = self.fabric.latest_optional(stream)
        if observation is None:
            raise RuntimeError(
                "USER_OBSTACLE_DESCRIPTION_REQUIRED: no scene segmentation "
                "policy has been published"
            )
        policy = parse_policy(observation)
        if self.policy is None or policy.identity != self.policy.identity:
            self.semantic_map.invalidate()
            self.policy = policy
            self.prompts = {}
            self.previous_masks = {}
            self.latest_angular_assertions = []
            self.latest_angular_projection = None
            self.latest_visible_surface_aabbs = []
            self.annotation_error = None
            self.last_quality_review = None
        return policy

    def _annotation_due(self, now: float) -> bool:
        if self.annotation_future is not None:
            return False
        if not self.prompts:
            return True
        started = self.annotation_started_monotonic
        return started is None or now - started >= self._vlm_refresh_interval()

    def _poll_annotation(self, policy: SceneSegmentationPolicy) -> bool:
        future = self.annotation_future
        if future is None or not future.done():
            return False
        self.annotation_future = None
        try:
            result = future.result()
            if self.annotation_policy_identity == policy.identity:
                self.prompts = result
                self.last_annotation_completed_monotonic = time.monotonic()
                self.annotation_error = None
                return True
        except Exception as error:
            self.annotation_error = str(error)
            self.last_annotation_completed_monotonic = time.monotonic()
        return False

    def _start_annotation(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
    ) -> None:
        self.annotation_policy_identity = policy.identity
        self.annotation_started_monotonic = time.monotonic()
        self.annotation_future = self.executor.submit(
            self.annotator.annotate,
            frame.rgb.copy(),
            policy,
        )

    def _map_identity(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        target_from_camera: dict[str, Any],
    ) -> str:
        stable_path = []
        for step in target_from_camera.get("path") or []:
            if not isinstance(step, dict):
                continue
            stable_path.append(
                {
                    key: step.get(key)
                    for key in (
                        "from_frame",
                        "to_frame",
                        "parent_frame",
                        "child_frame",
                        "direction",
                        "authority",
                        "provider_id",
                        "provider_instance_id",
                        "session_epoch",
                        "calibration_revision",
                    )
                }
            )
        transform_identity = hashlib.sha256(
            json.dumps(
                stable_path,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        values = [
            frame.camera_provider_id,
            frame.camera_provider_instance_id,
            frame.camera_boot_id,
            frame.calibration_revision,
            frame.session_epoch,
            policy.identity,
            "rebot_arm_base",
            transform_identity,
        ]
        if any(not str(value).strip() for value in values):
            raise RuntimeError("scene map identity inputs are incomplete")
        return ":".join(values)

    def _segment_current_frame(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        *,
        annotation_refreshed: bool,
    ) -> tuple[dict[str, np.ndarray], dict[str, float], list[str]]:
        self.tracker.set_image(frame.rgb)
        expected = [ARM_OBJECT_ID, *[value.object_id for value in policy.objects]]
        masks: dict[str, np.ndarray] = {}
        scores: dict[str, float] = {}
        errors: list[str] = []
        for object_id in expected:
            prompts = self.prompts.get(object_id) if annotation_refreshed else None
            if not prompts:
                previous = self.previous_masks.get(object_id)
                if previous is not None and previous.shape == frame.depth_m.shape:
                    try:
                        prompts = [prompt_from_mask(object_id, previous)]
                    except Exception as error:
                        errors.append(f"{object_id}: tracking prompt failed: {error}")
            if not prompts:
                prompts = self.prompts.get(object_id)
            if not prompts:
                errors.append(f"{object_id}: no VLM or tracking prompt")
                continue
            try:
                mask, score = self.tracker.segment(prompts)
                if mask.shape != frame.depth_m.shape:
                    mask = cv2.resize(
                        mask.astype(np.uint8),
                        (frame.depth_m.shape[1], frame.depth_m.shape[0]),
                        interpolation=cv2.INTER_NEAREST,
                    ) > 0
                masks[object_id] = np.ascontiguousarray(mask)
                scores[object_id] = score
            except Exception as error:
                errors.append(f"{object_id}: SAM2 failed: {error}")
        self.last_min_sam2_score = min(scores.values()) if scores else None
        return masks, scores, errors

    def _constrain_masks(
        self,
        frame: RgbdFrame,
        masks: dict[str, np.ndarray],
        *,
        annotation_refreshed: bool,
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        constrained: dict[str, np.ndarray] = {}
        diagnostics: dict[str, Any] = {}
        for object_id, mask in masks.items():
            prompts = self.prompts.get(object_id) if annotation_refreshed else None
            if not prompts:
                previous = self.previous_masks.get(object_id)
                if previous is not None and np.any(previous):
                    try:
                        prompts = [prompt_from_mask(object_id, previous)]
                    except Exception:
                        prompts = None
            if not prompts:
                prompts = self.prompts.get(object_id)
            if not prompts:
                diagnostics[object_id] = {"status": "PROMPT_REGION_UNAVAILABLE"}
                continue
            clean, detail = constrain_mask_to_prompted_depth_component(
                mask=mask,
                depth_m=frame.depth_m,
                boxes_yxyx=[value.box_yxyx for value in prompts],
                positive_points_yx=[
                    point
                    for value in prompts
                    for point in value.positive_points_yx
                ],
                box_padding_fraction=float(
                    self.config.get("prompt_box_padding_fraction", 0.03)
                ),
                local_depth_step_m=float(
                    self.config.get("semantic_depth_connectivity_m", 0.035)
                ),
                seed_search_radius_pixels=int(
                    self.config.get("positive_seed_search_radius_pixels", 24)
                ),
            )
            constrained[object_id] = clean
            diagnostics[object_id] = detail
        return constrained, diagnostics

    def _segment_masks(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        *,
        annotation_refreshed: bool,
    ) -> tuple[
        dict[str, np.ndarray],
        dict[str, float],
        list[str],
        dict[str, Any],
        bool,
    ]:
        masks, scores, errors = self._segment_current_frame(
            frame,
            policy,
            annotation_refreshed=annotation_refreshed,
        )
        masks, constraints = self._constrain_masks(
            frame,
            masks,
            annotation_refreshed=annotation_refreshed,
        )
        expected = {ARM_OBJECT_ID, *[value.object_id for value in policy.objects]}
        missing = sorted(
            object_id
            for object_id in expected
            if object_id not in masks or not np.any(masks[object_id])
        )
        accepted = not missing
        if accepted:
            self.previous_masks.update(masks)
        self.last_quality_review = {
            "status": (
                "SAM2_MASKS_ACCEPTED"
                if accepted
                else "SAM2_SEGMENTATION_INCOMPLETE"
            ),
            "post_sam2_vlm_review_performed": False,
            "missing_or_empty_masks": missing,
        }
        return masks, scores, errors, constraints, accepted

    @staticmethod
    def _encode_rgb(frame: RgbdFrame) -> bytes:
        image = cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2BGR)
        ok, payload = cv2.imencode(".png", image)
        if not ok:
            raise RuntimeError("could not encode SAM2 RGB visualization")
        return payload.tobytes()

    @staticmethod
    def _encode_depth(frame: RgbdFrame) -> bytes:
        depth = np.asarray(frame.depth_m, dtype=np.float32)
        valid = np.isfinite(depth) & (depth > 0.0)
        normalized = np.zeros(depth.shape, dtype=np.uint8)
        if np.any(valid):
            values = depth[valid]
            low, high = np.percentile(values, [2.0, 98.0])
            if float(high - low) <= 1e-6:
                normalized[valid] = 128
            else:
                scaled = (depth[valid] - low) / float(high - low)
                normalized[valid] = np.clip(scaled * 255.0, 0, 255).astype(
                    np.uint8
                )
        image = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
        image[~valid] = 0
        ok, payload = cv2.imencode(".png", image)
        if not ok:
            raise RuntimeError("could not encode SAM2 depth visualization")
        return payload.tobytes()

    @staticmethod
    def _overlay(
        frame: RgbdFrame,
        object_masks: dict[str, np.ndarray],
        arm_mask: np.ndarray,
        object_types: dict[str, str] | None = None,
    ) -> bytes:
        image = cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2BGR).astype(np.float32)
        type_colors = {
            "KEEP_OUT": np.asarray([32, 32, 240], dtype=np.float32),
            "PUSHABLE": np.asarray([220, 120, 32], dtype=np.float32),
            "WORK_OBJECT": np.asarray([32, 210, 230], dtype=np.float32),
        }
        arm_color = np.asarray([0, 140, 255], dtype=np.float32)
        legend: list[tuple[str, np.ndarray]] = []
        for object_id, mask in object_masks.items():
            object_type = str((object_types or {}).get(object_id) or "KEEP_OUT")
            color = type_colors.get(object_type, type_colors["KEEP_OUT"])
            selected = np.asarray(mask, dtype=bool)
            image[selected] = image[selected] * 0.45 + color * 0.55
            legend.append((f"{object_id} [{object_type}]", color))
        selected_arm = np.asarray(arm_mask, dtype=bool)
        image[selected_arm] = image[selected_arm] * 0.35 + arm_color * 0.65
        legend.append(("robot arm [excluded]", arm_color))
        rendered = np.clip(image, 0, 255).astype(np.uint8)
        for index, (label, color) in enumerate(legend):
            y = 26 + index * 28
            cv2.rectangle(
                rendered,
                (10, y - 16),
                (28, y + 2),
                tuple(int(value) for value in color),
                thickness=-1,
            )
            cv2.putText(
                rendered,
                label,
                (36, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
        ok, payload = cv2.imencode(".png", rendered)
        if not ok:
            raise RuntimeError("could not encode SAM2 visualization")
        return payload.tobytes()

    def tick(self) -> dict[str, Any] | None:
        started = time.monotonic()
        policy = self._load_policy()
        frame = self.capture.capture(
            attempts=int(self.config.get("buffer_ref_read_attempts", 6))
        )
        self.latest_rgb_png = self._encode_rgb(frame)
        self.latest_depth_png = self._encode_depth(frame)
        now = time.monotonic()
        self._update_visual_motion(frame, now)
        annotation_refreshed = self._poll_annotation(policy)
        if self._annotation_due(now):
            self._start_annotation(frame, policy)
        if not self.prompts and not self.previous_masks:
            self.last_diagnostics = {
                "status": "WAITING_FOR_INITIAL_VLM_ANNOTATION",
                "policy": policy.as_dict(),
                "annotation_error": self.annotation_error,
                "vlm_router": self._annotator_status(),
                "scene_motion_active": self.scene_motion_active,
            }
            return None

        (
            masks,
            scores,
            segmentation_errors,
            prompt_constraints,
            quality_accepted,
        ) = self._segment_masks(
            frame,
            policy,
            annotation_refreshed=annotation_refreshed,
        )
        if not quality_accepted:
            arm_mask = masks.get(ARM_OBJECT_ID)
            declared = {
                value.object_id: masks[value.object_id]
                for value in policy.objects
                if value.object_id in masks
            }
            if arm_mask is not None:
                self.latest_visualization_png = self._overlay(
                    frame,
                    declared,
                    arm_mask,
                    {
                        value.object_id: value.object_type
                        for value in policy.objects
                    },
                )
            self.last_diagnostics = {
                "status": "SAM2_SEGMENTATION_INCOMPLETE",
                "policy": policy.as_dict(),
                "quality_review": self.last_quality_review,
                "prompt_depth_constraints": prompt_constraints,
                "sam2_scores": scores,
                "segmentation_errors": segmentation_errors,
                "vlm_router": self._annotator_status(),
            }
            return self._publish_mapping_failure(
                frame,
                policy,
                failure_status="SAM2_SEGMENTATION_INCOMPLETE",
                attempt_count=1,
                failure_details={
                    "quality_review": self.last_quality_review,
                    "prompt_depth_constraints": prompt_constraints,
                    "sam2_scores": scores,
                    "segmentation_errors": segmentation_errors,
                },
            )
        arm_mask = masks.get(ARM_OBJECT_ID)
        if arm_mask is None:
            try:
                target_from_camera = self.fabric.transform(
                    from_frame=frame.camera_frame,
                    to_frame="rebot_arm_base",
                    at_us=frame.observed_at_us,
                    max_extrapolation_us=int(
                        self.config.get(
                            "maximum_transform_extrapolation_us",
                            750_000,
                        )
                    ),
                )
                identity_reset = self.semantic_map.bind_identity(
                    self._map_identity(frame, policy, target_from_camera)
                )
            except Exception as error:
                return self._publish_mapping_failure(
                    frame,
                    policy,
                    failure_status=(
                        "ARM_MASK_AND_CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE"
                    ),
                    attempt_count=0,
                    failure_details={
                        "transform_error": str(error),
                        "from_frame": frame.camera_frame,
                        "to_frame": "rebot_arm_base",
                        "blocking_prerequisite": {
                            "status": "TRANSFORM_UNAVAILABLE",
                            "requires_external_action": True,
                            "from_frame": frame.camera_frame,
                            "to_frame": "rebot_arm_base",
                            "message": (
                                "Establish or restore the current camera-to-arm-base "
                                "calibration before requesting 3D semantic geometry."
                            ),
                        },
                        "segmentation_errors": segmentation_errors,
                        "annotation_error": self.annotation_error,
                        "vlm_router": self._annotator_status(),
                    },
                )
            self.last_diagnostics = {
                "status": "ARM_MASK_UNAVAILABLE_RETAINING_PRIOR_MAP",
                "policy": policy.as_dict(),
                "segmentation_errors": segmentation_errors,
                "annotation_error": self.annotation_error,
                "map_identity_reset": identity_reset,
            }
            return self._publish_retained_map(frame, policy, started)

        declared_masks = {
            value.object_id: masks[value.object_id]
            for value in policy.objects
            if value.object_id in masks
        }
        valid_depth = (
            np.isfinite(frame.depth_m)
            & (frame.depth_m >= float(self.config.get("minimum_depth_m", 0.05)))
            & (frame.depth_m <= float(self.config.get("maximum_depth_m", 5.0)))
        )
        partition = partition_semantic_masks(
            policy=policy,
            declared_masks=declared_masks,
            arm_mask=arm_mask,
            valid_depth_mask=valid_depth,
            arm_dilation_pixels=int(self.config.get("arm_mask_dilation_pixels", 18)),
        )
        self.latest_visualization_png = self._overlay(
            frame,
            partition.object_masks,
            partition.dilated_arm_mask,
            {
                value.object_id: value.object_type
                for value in policy.objects
            },
        )
        try:
            target_from_camera = self.fabric.transform(
                from_frame=frame.camera_frame,
                to_frame="rebot_arm_base",
                at_us=frame.observed_at_us,
                max_extrapolation_us=int(
                    self.config.get("maximum_transform_extrapolation_us", 750_000)
                ),
            )
        except Exception as error:
            return self._publish_mapping_failure(
                frame,
                policy,
                failure_status=(
                    "CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE_2D_TRACKING_ACTIVE"
                ),
                attempt_count=0,
                failure_details={
                    "transform_error": str(error),
                    "from_frame": frame.camera_frame,
                    "to_frame": "rebot_arm_base",
                    "tracking_mode": "2D_MASK_TRACKING_ACTIVE",
                    "blocking_prerequisite": {
                        "status": "TRANSFORM_UNAVAILABLE",
                        "requires_external_action": True,
                        "from_frame": frame.camera_frame,
                        "to_frame": "rebot_arm_base",
                        "message": (
                            "Establish or restore the current camera-to-arm-base "
                            "calibration before requesting 3D semantic geometry."
                        ),
                    },
                    "mask_partition": partition.diagnostics,
                    "prompt_depth_constraints": prompt_constraints,
                    "quality_review": self.last_quality_review,
                    "sam2_scores": scores,
                    "segmentation_errors": segmentation_errors,
                    "annotation_error": self.annotation_error,
                    "annotation_refreshed": annotation_refreshed,
                    "vlm_router": self._annotator_status(),
                    "scene_motion_active": self.scene_motion_active,
                    "visual_motion_score": self.visual_motion_score,
                },
            )
        identity_reset = self.semantic_map.bind_identity(
            self._map_identity(frame, policy, target_from_camera)
        )
        updates: dict[str, Any] = {}
        semantic_surfaces: list[dict[str, Any]] = []
        visible_surface_aabbs: list[dict[str, Any]] = []
        mask_erosion: dict[str, Any] = {}
        descriptions = {value.object_id: value for value in policy.objects}
        for object_id, mask in partition.object_masks.items():
            description = descriptions[object_id]
            erosion_m = {
                "KEEP_OUT": float(
                    self.config.get("keep_out_mask_erosion_m", 0.02)
                ),
                "WORK_OBJECT": float(
                    self.config.get("work_object_mask_erosion_m", 0.01)
                ),
            }.get(description.object_type, 0.0)
            geometry_mask, erosion_diagnostics = erode_mask_by_metric_boundary(
                mask=mask,
                depth_m=frame.depth_m,
                intrinsics=frame.intrinsics,
                erosion_m=erosion_m,
                minimum_depth_m=float(self.config.get("minimum_depth_m", 0.05)),
                maximum_depth_m=float(self.config.get("maximum_depth_m", 5.0)),
            )
            mask_erosion[object_id] = {
                "type": description.object_type,
                **erosion_diagnostics,
            }
            points = project_masked_depth_to_frame(
                depth_m=frame.depth_m,
                mask=geometry_mask,
                intrinsics=frame.intrinsics,
                target_from_camera=target_from_camera,
                pixel_stride=int(self.config.get("depth_pixel_stride", 2)),
                minimum_depth_m=float(self.config.get("minimum_depth_m", 0.05)),
                maximum_depth_m=float(self.config.get("maximum_depth_m", 5.0)),
            )
            points = points[np.linalg.norm(points, axis=1) <= 1.2]
            semantic_surfaces.append(
                {
                    "object_id": object_id,
                    "type": description.object_type,
                    "description": description.description,
                    "points_m": points,
                }
            )
            if description.object_type == "WORK_OBJECT":
                aabb = build_visible_surface_aabb(
                    object_id=object_id,
                    object_type=description.object_type,
                    description=description.description,
                    points_m=points,
                    observed_at_us=frame.observed_at_us,
                    freshness_ms=int(
                        self.config.get("aabb_freshness_ms", 5000)
                    ),
                    source_frame_number=frame.frame_number,
                    source_policy_revision=policy.revision,
                )
                if aabb is not None:
                    visible_surface_aabbs.append(aabb)
            updates[object_id] = self.semantic_map.update(
                object_id=object_id,
                object_type=description.object_type,
                description=description.description,
                points_m=points,
                observed_at_us=frame.observed_at_us,
                surface_viewpoint_m=np.asarray(
                    target_from_camera.get("translation_m"),
                    dtype=np.float64,
                ),
            )
        gripper = self._gripper_center(frame)
        direction_count = int(self.config.get("angular_direction_count", 4096))
        angular_radius_scale = float(
            self.config.get("angular_radius_scale", 1.5)
        )
        angular_minimum_radius_m = float(
            self.config.get("angular_minimum_radius_m", 0.005)
        )
        angular_radial_padding_m = float(
            self.config.get("angular_radial_padding_m", 0.003)
        )
        angular_maximum_range_m = float(
            self.config.get("angular_maximum_range_m", 1.2)
        )
        self.latest_angular_assertions = build_hand_angular_assertions(
            semantic_surfaces,
            hand_center_m=gripper,
            direction_count=direction_count,
            angular_radius_scale=angular_radius_scale,
            minimum_radius_m=angular_minimum_radius_m,
            radial_padding_m=angular_radial_padding_m,
            maximum_range_m=angular_maximum_range_m,
            include_pushable=bool(
                self.config.get("publish_pushable_geometry", False)
            ),
            maximum_assertions=int(self.config.get("maximum_assertions", 20_000)),
        )
        self.latest_angular_projection = hand_angular_projection_metadata(
            hand_center_m=gripper,
            observed_at_us=frame.observed_at_us,
            direction_count=direction_count,
            occupied_direction_count=len(self.latest_angular_assertions),
            angular_radius_scale=angular_radius_scale,
            minimum_radius_m=angular_minimum_radius_m,
            radial_padding_m=angular_radial_padding_m,
            maximum_range_m=angular_maximum_range_m,
        )
        self.latest_visible_surface_aabbs = visible_surface_aabbs
        return self._publish(
            frame,
            policy,
            started,
            extra_diagnostics={
                "status": "TRACKED_SEMANTIC_MAP_PUBLISHED",
                "mask_partition": partition.diagnostics,
                "mask_erosion": mask_erosion,
                "prompt_depth_constraints": prompt_constraints,
                "quality_review": self.last_quality_review,
                "sam2_scores": scores,
                "segmentation_errors": segmentation_errors,
                "annotation_error": self.annotation_error,
                "annotation_refreshed": annotation_refreshed,
                "map_identity_reset": identity_reset,
                "fusion_updates": updates,
                "vlm_router": self._annotator_status(),
                "scene_motion_active": self.scene_motion_active,
                "visual_motion_score": self.visual_motion_score,
            },
        )

    def _gripper_center(self, frame: RgbdFrame) -> np.ndarray:
        transform = self.fabric.transform(
            from_frame=str(self.config.get("arm_tool_frame") or "rebot_arm_tool"),
            to_frame="rebot_arm_base",
            at_us=frame.observed_at_us,
            max_extrapolation_us=int(
                self.config.get("maximum_transform_extrapolation_us", 750_000)
            ),
        )
        center = np.asarray(transform.get("translation_m"), dtype=np.float64)
        if center.shape != (3,) or not np.all(np.isfinite(center)):
            raise RuntimeError("current gripper transform is invalid")
        return center

    def _coverage(self, policy: SceneSegmentationPolicy) -> dict[str, Any]:
        snapshot = self.semantic_map.snapshot()
        objects = snapshot.get("objects") or {}
        missing = [
            value.object_id
            for value in policy.blocking_objects
            if int((objects.get(value.object_id) or {}).get("persistent_voxel_count") or 0)
            <= 0
        ]
        return {
            "ready": not missing,
            "missing_blocking_objects": missing,
            "required_blocking_objects": [
                value.object_id for value in policy.blocking_objects
            ],
        }

    def _publish_mapping_failure(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        *,
        failure_status: str,
        attempt_count: int,
        failure_details: dict[str, Any],
    ) -> dict[str, Any]:
        """Invalidate the current policy revision without reusing old geometry."""

        self.latest_angular_assertions = []
        self.latest_angular_projection = None
        self.latest_visible_surface_aabbs = []
        self.sequence += 1
        now_us = time.time_ns() // 1000
        freshness_ms = int(self.config.get("assertion_freshness_ms", 3000))
        missing = [value.object_id for value in policy.blocking_objects]
        observation = {
            "schema": "physical_agent.arm_semantic_assertions",
            "schema_version": 1,
            "stream": str(
                self.config.get("output_stream")
                or "robot_arm.scene.tracked_semantic_assertions"
            ),
            "provider_id": self.provider_id,
            "provider_instance_id": self.provider_instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": now_us,
            "freshness_ms": freshness_ms,
            "expires_at_us": now_us + freshness_ms * 1000,
            "coordinate_frame": "rebot_arm_base",
            "clock_domain": "system",
            "valid": False,
            "data": {
                "contract_version": 1,
                "frame_id": "rebot_arm_base",
                "policy": policy.as_dict(),
                "map_identity": (
                    self.semantic_map.identity or f"unfused:{policy.identity}"
                ),
                "coverage": {
                    "ready": False,
                    "missing_blocking_objects": missing,
                    "required_blocking_objects": missing,
                    "failure_status": failure_status,
                    "attempt_count": int(attempt_count),
                },
                "assertions": [],
                "visible_surface_aabbs": [],
                "mapping_failure": {
                    "status": failure_status,
                    **failure_details,
                },
            },
        }
        publish_result = self.fabric.publish(observation)
        self.last_observation = observation
        self.last_diagnostics = {
            "status": failure_status,
            "policy": policy.as_dict(),
            "mapping_failure": observation["data"]["mapping_failure"],
            "publish_result": publish_result,
            "source_frame_number": frame.frame_number,
            "source_observed_at_us": frame.observed_at_us,
        }
        blocking_prerequisite = failure_details.get("blocking_prerequisite")
        if isinstance(blocking_prerequisite, dict):
            self.last_diagnostics["blocking_prerequisite"] = blocking_prerequisite
        return observation

    def _publish_retained_map(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        started: float,
    ) -> dict[str, Any] | None:
        if not self._coverage(policy)["ready"]:
            return None
        return self._publish(
            frame,
            policy,
            started,
            extra_diagnostics={
                "status": "OCCLUDED_OR_ARM_MASK_MISSING_REPUBLISHED_RETAINED_MAP",
            },
        )

    def _publish(
        self,
        frame: RgbdFrame,
        policy: SceneSegmentationPolicy,
        started: float,
        *,
        extra_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        assertions = list(self.latest_angular_assertions)
        coverage = self._coverage(policy)
        self.sequence += 1
        now_us = time.time_ns() // 1000
        freshness_ms = int(self.config.get("assertion_freshness_ms", 3000))
        visible_surface_aabbs = [
            value
            for value in self.latest_visible_surface_aabbs
            if now_us <= int(value.get("expires_at_us") or 0)
        ]
        observation = {
            "schema": "physical_agent.arm_semantic_assertions",
            "schema_version": 1,
            "stream": str(
                self.config.get("output_stream")
                or "robot_arm.scene.tracked_semantic_assertions"
            ),
            "provider_id": self.provider_id,
            "provider_instance_id": self.provider_instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": now_us,
            "freshness_ms": freshness_ms,
            "expires_at_us": now_us + freshness_ms * 1000,
            "coordinate_frame": "rebot_arm_base",
            "clock_domain": "system",
            "valid": bool(coverage["ready"]),
            "data": {
                "contract_version": 1,
                "frame_id": "rebot_arm_base",
                "policy": policy.as_dict(),
                "map_identity": self.semantic_map.identity,
                "coverage": coverage,
                "assertions": assertions,
                "angular_projection": self.latest_angular_projection,
                "visible_surface_aabbs": visible_surface_aabbs,
            },
        }
        result = self.fabric.publish(observation)
        self.last_observation = observation
        self.last_diagnostics = {
            **extra_diagnostics,
            "coverage": coverage,
            "policy": policy.as_dict(),
            "semantic_map": self.semantic_map.snapshot(),
            "assertion_count": len(assertions),
            "angular_profile": ANGULAR_PROFILE_ID,
            "angular_direction_count": int(
                self.config.get("angular_direction_count", 4096)
            ),
            "visible_surface_aabb_count": len(visible_surface_aabbs),
            "source_frame_number": frame.frame_number,
            "source_observed_at_us": frame.observed_at_us,
            "publish_result": result,
            "tick_elapsed_ms": (time.monotonic() - started) * 1000.0,
            "vlm_refresh_interval_s": self._vlm_refresh_interval(),
            "tracking_rate_policy_hz": {
                "mode": "FIXED_CONFIGURED",
                "configured": float(self.config.get("tracking_rate_hz", 1.0)),
            },
            "unclaimed_visible_policy": "PUSHABLE_IGNORED",
        }
        return observation

    def close(self) -> None:
        future = self.annotation_future
        if future is not None:
            future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.tracker.close()
        self.annotator.close()
