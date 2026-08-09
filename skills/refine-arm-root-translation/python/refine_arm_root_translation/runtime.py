from __future__ import annotations

import asyncio
import copy
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import numpy as np

from .geometry import rigid_transform
from .landmark import (
    InvalidDepthSelectionError,
    build_invalid_depth_retry_prompt,
    build_landmark_prompt,
    parse_landmark_detection,
    resolve_profile_landmark,
)
from .profile import (
    load_effector_profile,
    resolve_tool_landmark_point,
    select_visual_landmark,
)
from .refinement import (
    apply_compact_translation_update,
    finalize_translation_refinement,
    prepare_translation_refinement,
)
from .review import build_quality_review_prompt, parse_quality_review
from .visual import (
    build_alignment_image_projections,
    build_detection_annotations,
    build_landmark_review_crop_annotations,
    build_rgbd_visual_channels,
    build_visual_annotations,
    render_marked_overlap_png,
    render_landmark_review_crop_png,
)


class VlmClient(Protocol):
    model_id: str

    async def invoke(
        self,
        *,
        prompt: str,
        images: list[dict[str, Any]],
        purpose: str,
        request_id: str,
    ) -> str | dict[str, Any]: ...


class CompactStateStore(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...

    async def compare_and_swap(
        self,
        *,
        expected_revision: int,
        state: dict[str, Any],
        refinement: dict[str, Any],
    ) -> bool: ...


class VisualEvidencePublisher(Protocol):
    async def register_channels(self, **kwargs: Any) -> dict[str, Any]: ...


ObservationSource = Callable[[], Awaitable[dict[str, Any]]]
StateRevalidator = Callable[
    [dict[str, Any]],
    Awaitable[dict[str, Any]],
]
ReferenceImageSource = Callable[
    [list[str]],
    Awaitable[list[dict[str, Any]]],
]


class TranslationRefinementSkill:
    """Finite orchestration for one timestamp-coherent XYZ refinement call."""

    def __init__(
        self,
        *,
        profile_path: str | Path,
        observation_source: ObservationSource,
        state_revalidator: StateRevalidator,
        vlm: VlmClient,
        state_store: CompactStateStore,
        visual_evidence_publisher: VisualEvidencePublisher,
        reference_image_source: ReferenceImageSource | None = None,
        review_threshold_m: float = 0.005,
        maximum_raw_translation_delta_m: float = 0.1,
        maximum_adopted_translation_delta_m: float = 0.025,
        minimum_confidence: float = 0.75,
        minimum_same_surface_confidence: float = 0.75,
        maximum_capture_landmark_motion_m: float = 0.005,
    ) -> None:
        self.profile = load_effector_profile(profile_path)
        self.observation_source = observation_source
        self.state_revalidator = state_revalidator
        self.vlm = vlm
        self.state_store = state_store
        self.visual_evidence_publisher = visual_evidence_publisher
        self.reference_image_source = reference_image_source
        self.review_threshold_m = float(review_threshold_m)
        self.maximum_raw_translation_delta_m = float(
            maximum_raw_translation_delta_m
        )
        self.maximum_adopted_translation_delta_m = float(
            maximum_adopted_translation_delta_m
        )
        self.minimum_confidence = float(minimum_confidence)
        self.minimum_same_surface_confidence = float(
            minimum_same_surface_confidence
        )
        self.maximum_capture_landmark_motion_m = float(
            maximum_capture_landmark_motion_m
        )
        if (
            not np.isfinite(self.review_threshold_m)
            or self.review_threshold_m < 0.0
        ):
            raise ValueError("review_threshold_m must be non-negative")
        if (
            not np.isfinite(self.maximum_raw_translation_delta_m)
            or self.maximum_raw_translation_delta_m <= 0.0
        ):
            raise ValueError(
                "maximum_raw_translation_delta_m must be positive"
            )
        if (
            not np.isfinite(self.maximum_adopted_translation_delta_m)
            or self.maximum_adopted_translation_delta_m <= 0.0
        ):
            raise ValueError(
                "maximum_adopted_translation_delta_m must be positive"
            )
        if self.review_threshold_m > self.maximum_raw_translation_delta_m:
            raise ValueError(
                "review_threshold_m must not exceed the raw-delta limit"
            )
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be from zero to one")
        if not 0.0 <= self.minimum_same_surface_confidence <= 1.0:
            raise ValueError(
                "minimum_same_surface_confidence must be from zero to one"
            )
        if (
            not np.isfinite(self.maximum_capture_landmark_motion_m)
            or self.maximum_capture_landmark_motion_m < 0.0
        ):
            raise ValueError(
                "maximum_capture_landmark_motion_m must be non-negative"
            )

    async def run(
        self,
        *,
        adoption_factor: float = 1.0,
        sample_count: int = 1,
        landmark_id: str | None = None,
    ) -> dict[str, Any]:
        factor = self._validate_adoption_factor(adoption_factor)
        count = self._validate_sample_count(sample_count)
        run_id = f"arm-root-refinement-{uuid.uuid4().hex}"
        if count == 1:
            result = await self._run_single(
                adoption_factor=factor,
                landmark_id=landmark_id,
                request_id_prefix=f"{run_id}/sample-01",
            )
            return self._attach_single_sample_metadata(result)
        return await self._run_multi_sample(
            adoption_factor=factor,
            sample_count=count,
            landmark_id=landmark_id,
            run_id=run_id,
        )

    async def _run_single(
        self,
        *,
        adoption_factor: float,
        landmark_id: str | None = None,
        apply_state_update: bool = True,
        enforce_delta_limits: bool = True,
        state_override: dict[str, Any] | None = None,
        observation_override: dict[str, Any] | None = None,
        reference_images_override: list[dict[str, Any]] | None = None,
        request_id_prefix: str | None = None,
    ) -> dict[str, Any]:
        state = (
            copy.deepcopy(state_override)
            if state_override is not None
            else await self.state_store.snapshot()
        )
        observation = (
            observation_override
            if observation_override is not None
            else await self.observation_source()
        )
        invocation_records: list[dict[str, Any]] = []
        request_prefix = request_id_prefix or (
            f"arm-root-refinement-{uuid.uuid4().hex}/sample-01"
        )
        self._validate_capture_observation(observation, state)
        landmark = select_visual_landmark(self.profile, landmark_id)
        tool_point = resolve_tool_landmark_point(
            landmark,
            runtime_bindings=observation.get("runtime_landmark_bindings"),
        )
        capture_motion = self._validate_capture_motion(
            observation,
            tool_point=tool_point,
        )
        rgb = np.asarray(observation["rgb"])
        depth = np.asarray(observation["registered_depth_m"], dtype=np.float64)
        channels, overlap = build_rgbd_visual_channels(rgb, depth)
        reference_images = (
            reference_images_override
            if reference_images_override is not None
            else await self._load_reference_images(landmark)
        )
        first_prompt = build_landmark_prompt(
            profile=self.profile,
            landmark=landmark,
            rgb_grid=rgb.shape[:2],
            registered_depth_grid=depth.shape,
        )
        try:
            first_response = await self._invoke_vlm(
                prompt=first_prompt,
                images=[
                    self._vlm_image(channel)
                    for channel in channels
                    if channel["id"]
                    in {"rgb", "depth", "depth_validity", "rgb_depth"}
                ]
                + reference_images,
                purpose="EFFECTOR_LANDMARK_TRANSLATION_REFINEMENT",
                request_id=f"{request_prefix}/detect",
                invocation_records=invocation_records,
            )
            detection = parse_landmark_detection(
                first_response,
                landmark=landmark,
                rgb_grid=rgb.shape[:2],
                registered_depth_grid=depth.shape,
            )
        except (RuntimeError, ValueError) as error:
            detection = {
                "schema": "midbrain.effector_landmark_detection",
                "schema_version": 2,
                "scene_suitable": False,
                "landmark_id": landmark["landmark_id"],
                "coordinate_space": "NORMALIZED_YX_0_1000_PER_IMAGE",
                "reason": f"landmark VLM output rejected: {error}",
                "points": [],
            }
        depth_reselection = {
            "required": False,
            "attempt_count": 1,
            "outcome": "NOT_REQUIRED",
            "initial_invalid_points": [],
        }
        if not detection["scene_suitable"]:
            marked_overlap = render_marked_overlap_png(
                overlap,
                detection=detection,
                resolved_landmark={},
            )
            channels.append(
                self._marked_overlap_channel(marked_overlap, depth.shape)
            )
            visual_evidence = await self._publish_evidence(
                channels=channels,
                annotations=[],
                landmark=landmark,
                confidence="low",
            )
            return self._attach_vlm_invocations(
                {
                    "schema": "midbrain.arm_root_translation_refinement",
                    "schema_version": 1,
                    "status": "REJECTED_OBSERVATION",
                    "workflow_complete": True,
                    "eligible_for_state_update": False,
                    "reason": detection["reason"],
                    "landmark_depth_reselection": depth_reselection,
                    "visual_evidence": visual_evidence,
                    "physical_motion_submitted": False,
                    "physical_motion_authorized": False,
                },
                invocation_records,
            )
        depth_retry_error: str | None = None
        retry_annotations: list[dict[str, Any]] = []
        try:
            resolved = resolve_profile_landmark(
                detection=detection,
                landmark=landmark,
                rgb_grid=rgb.shape[:2],
                registered_depth_m=depth,
                intrinsics=observation["intrinsics"],
                world_from_camera=np.asarray(observation["world_from_camera"]),
                minimum_confidence=self.minimum_confidence,
                minimum_same_surface_confidence=(
                    self.minimum_same_surface_confidence
                ),
            )
        except InvalidDepthSelectionError as error:
            depth_reselection = {
                "required": True,
                "attempt_count": 2,
                "outcome": "PENDING",
                "initial_invalid_points": error.invalid_points,
            }
            first_detection = detection
            retry_marked_overlap = render_marked_overlap_png(
                overlap,
                detection=first_detection,
                resolved_landmark={},
            )
            retry_channel = {
                "id": "invalid_depth_retry_input",
                "label": "Rejected VLM depth selections sent for correction",
                "image_bytes": retry_marked_overlap,
                "media_type": "image/png",
                "width": int(depth.shape[1]),
                "height": int(depth.shape[0]),
            }
            channels.append(retry_channel)
            retry_annotations = [
                {
                    **annotation,
                    "id": str(annotation["id"]) + "-rejected-selection",
                    "label": "Rejected " + str(annotation["label"]),
                    "applies_to_channels": ["invalid_depth_retry_input"],
                }
                for annotation in build_detection_annotations(
                    detection=first_detection,
                    rgb_grid=rgb.shape[:2],
                    registered_depth_grid=depth.shape,
                )
                if str(annotation["id"]).endswith("-depth")
            ]
            retry_prompt = build_invalid_depth_retry_prompt(
                profile=self.profile,
                landmark=landmark,
                rgb_grid=rgb.shape[:2],
                registered_depth_grid=depth.shape,
                invalid_points=error.invalid_points,
            )
            try:
                retry_response = await self._invoke_vlm(
                    prompt=retry_prompt,
                    images=[
                        self._vlm_image(channel)
                        for channel in channels
                        if channel["id"]
                        in {
                            "rgb",
                            "depth",
                            "depth_validity",
                            "rgb_depth",
                            "invalid_depth_retry_input",
                        }
                    ]
                    + reference_images,
                    purpose="EFFECTOR_LANDMARK_DEPTH_RESELECTION",
                    request_id=f"{request_prefix}/depth-reselection",
                    invocation_records=invocation_records,
                )
                detection = parse_landmark_detection(
                    retry_response,
                    landmark=landmark,
                    rgb_grid=rgb.shape[:2],
                    registered_depth_grid=depth.shape,
                )
                if not detection["scene_suitable"]:
                    raise RuntimeError(
                        "depth-reselection VLM rejected the landmark scene: "
                        + detection["reason"]
                    )
                resolved = resolve_profile_landmark(
                    detection=detection,
                    landmark=landmark,
                    rgb_grid=rgb.shape[:2],
                    registered_depth_m=depth,
                    intrinsics=observation["intrinsics"],
                    world_from_camera=np.asarray(
                        observation["world_from_camera"]
                    ),
                    minimum_confidence=self.minimum_confidence,
                    minimum_same_surface_confidence=(
                        self.minimum_same_surface_confidence
                    ),
                )
                depth_reselection["outcome"] = "VALID_EXACT_DEPTH"
            except (RuntimeError, ValueError) as retry_error:
                depth_reselection["outcome"] = "REJECTED"
                depth_retry_error = (
                    f"initial depth selection rejected: {error}; "
                    f"one VLM depth-reselection attempt failed: {retry_error}"
                )
        except (RuntimeError, ValueError) as error:
            depth_retry_error = str(error)
        if depth_retry_error is not None:
            marked_overlap = render_marked_overlap_png(
                overlap,
                detection=detection,
                resolved_landmark={},
            )
            channels.append(
                self._marked_overlap_channel(marked_overlap, depth.shape)
            )
            annotations = retry_annotations + build_detection_annotations(
                detection=detection,
                rgb_grid=rgb.shape[:2],
                registered_depth_grid=depth.shape,
            )
            visual_evidence = await self._publish_evidence(
                channels=channels,
                annotations=annotations,
                landmark=landmark,
                confidence="low",
            )
            return self._attach_vlm_invocations(
                {
                    "schema": "midbrain.arm_root_translation_refinement",
                    "schema_version": 1,
                    "status": "REJECTED_OBSERVATION",
                    "workflow_complete": True,
                    "eligible_for_state_update": False,
                    "reason": depth_retry_error,
                    "landmark_depth_reselection": depth_reselection,
                    "visual_evidence": visual_evidence,
                    "physical_motion_submitted": False,
                    "physical_motion_authorized": False,
                },
                invocation_records,
            )
        if not resolved["eligible_for_translation_refinement"]:
            marked_overlap = render_marked_overlap_png(
                overlap,
                detection=detection,
                resolved_landmark=resolved,
            )
            channels.append(
                self._marked_overlap_channel(marked_overlap, depth.shape)
            )
            annotations = retry_annotations + build_visual_annotations(
                detection=detection,
                resolved_landmark=resolved,
                rgb_grid=rgb.shape[:2],
                registered_depth_grid=depth.shape,
            )
            visual_evidence = await self._publish_evidence(
                channels=channels,
                annotations=annotations,
                landmark=landmark,
                confidence="low",
            )
            return self._attach_vlm_invocations(
                {
                    "schema": "midbrain.arm_root_translation_refinement",
                    "schema_version": 1,
                    "status": "REJECTED_OBSERVATION",
                    "workflow_complete": True,
                    "eligible_for_state_update": False,
                    "reason": "; ".join(resolved["quality_reasons"]),
                    "landmark_depth_reselection": depth_reselection,
                    "resolved_landmark": resolved,
                    "visual_evidence": visual_evidence,
                    "physical_motion_submitted": False,
                    "physical_motion_authorized": False,
                },
                invocation_records,
            )
        refinement = prepare_translation_refinement(
            active_world_from_base=state["world_from_base"],
            base_from_tool=observation["base_from_tool"],
            tool_landmark_point_m=tool_point,
            observed_world_landmark_point_m=resolved["world_landmark_point_m"],
            adoption_factor=adoption_factor,
            review_threshold_m=self.review_threshold_m,
            source_revision=int(state["revision"]),
            identities=state["identities"],
            landmark_id=landmark["landmark_id"],
            observation_provenance=observation["provenance"],
        )
        alignment_projections = build_alignment_image_projections(
            source_world_from_base=refinement["source_world_from_base"],
            proposed_world_from_base=refinement["proposed_world_from_base"],
            base_from_tool=refinement["base_from_tool"],
            tool_landmark_point_m=refinement["tool_landmark_point_m"],
            world_from_camera=observation["world_from_camera"],
            intrinsics=observation["intrinsics"],
            registered_depth_grid=depth.shape,
        )
        marked_overlap = render_marked_overlap_png(
            overlap,
            detection=detection,
            resolved_landmark=resolved,
            alignment_projections=alignment_projections,
        )
        channels.append(
            self._marked_overlap_channel(marked_overlap, depth.shape)
        )
        annotations = retry_annotations + build_visual_annotations(
            detection=detection,
            resolved_landmark=resolved,
            rgb_grid=rgb.shape[:2],
            registered_depth_grid=depth.shape,
            alignment_projections=alignment_projections,
        )
        quality_review_evidence = {
            "channel_ids": [],
            "crop_panels": [],
        }
        if refinement["quality_review"]["required"]:
            review_crop, crop_panels, crop_grid = (
                render_landmark_review_crop_png(
                    marked_overlap,
                    detection=detection,
                    registered_depth_grid=depth.shape,
                )
            )
            channels.append(
                {
                    "id": "landmark_review_crop",
                    "label": (
                        "Magnified marked landmark region for quality review"
                    ),
                    "image_bytes": review_crop,
                    "media_type": "image/png",
                    "width": int(crop_grid[1]),
                    "height": int(crop_grid[0]),
                }
            )
            annotations.extend(
                build_landmark_review_crop_annotations(
                    crop_panels=crop_panels,
                    crop_grid=crop_grid,
                )
            )
            quality_review_evidence = {
                "channel_ids": [
                    "rgb",
                    "depth",
                    "depth_validity",
                    "marked_overlap",
                    "landmark_review_crop",
                ],
                "crop_panels": crop_panels,
            }
        visual_evidence = await self._publish_evidence(
            channels=channels,
            annotations=annotations,
            landmark=landmark,
            confidence="high",
        )
        refinement["resolved_landmark"] = resolved
        refinement["landmark_depth_reselection"] = depth_reselection
        refinement["visual_evidence"] = visual_evidence
        refinement["quality_review_evidence"] = quality_review_evidence
        refinement["capture_motion"] = capture_motion
        refinement["vlm_invocations"] = invocation_records
        refinement["alignment_image_back_projection"] = {
            "camera_model": "PINHOLE_REGISTERED_DEPTH_INTRINSICS",
            "registered_depth_grid": [int(depth.shape[0]), int(depth.shape[1])],
            "points": alignment_projections,
        }
        raw_delta_norm_m = float(refinement["raw_translation_delta_norm_m"])
        adopted_delta_norm_m = float(
            np.linalg.norm(refinement["adopted_translation_delta_m"])
        )
        refinement["adopted_translation_delta_norm_m"] = adopted_delta_norm_m
        refinement["refinement_limits"] = {
            "maximum_raw_translation_delta_m": (
                self.maximum_raw_translation_delta_m
            ),
            "maximum_adopted_translation_delta_m": (
                self.maximum_adopted_translation_delta_m
            ),
        }
        delta_limit_exceeded = float(refinement["adoption_factor"]) > 0.0 and (
            raw_delta_norm_m > self.maximum_raw_translation_delta_m
            or adopted_delta_norm_m > self.maximum_adopted_translation_delta_m
        )
        if refinement["quality_review"]["required"]:
            review_prompt = build_quality_review_prompt(
                profile=self.profile,
                landmark=landmark,
                raw_translation_delta_m=refinement["raw_translation_delta_m"],
                raw_translation_delta_norm_m=(
                    refinement["raw_translation_delta_norm_m"]
                ),
            )
            try:
                review_response = await self._invoke_vlm(
                    prompt=review_prompt,
                    images=[
                        self._vlm_image(channel)
                        for channel in channels
                        if channel["id"]
                        in set(quality_review_evidence["channel_ids"])
                    ]
                    + reference_images,
                    purpose="EFFECTOR_LANDMARK_MARKING_QUALITY_REVIEW",
                    request_id=f"{request_prefix}/quality-review",
                    invocation_records=invocation_records,
                )
                review = parse_quality_review(
                    review_response,
                    landmark=landmark,
                )
            except (RuntimeError, ValueError) as error:
                review = {
                    "schema": "midbrain.effector_landmark_quality_review",
                    "schema_version": 1,
                    "landmark_id": landmark["landmark_id"],
                    "verdict": "UNRESOLVED",
                    "reason": f"quality-review VLM output rejected: {error}",
                    "reviewed_point_ids": list(
                        landmark["required_point_ids"]
                    ),
                }
            refinement = finalize_translation_refinement(
                refinement,
                quality_review=review,
            )
        if enforce_delta_limits and delta_limit_exceeded:
            refinement["status"] = "REJECTED_DELTA_LIMIT"
            refinement["workflow_complete"] = True
            refinement["eligible_for_state_update"] = False
            refinement["reason"] = (
                "raw translation delta exceeds the effector-profile limit"
                if raw_delta_norm_m > self.maximum_raw_translation_delta_m
                else (
                    "adopted translation delta exceeds the effector-profile "
                    "per-call limit; reduce adoption_factor"
                )
            )
            refinement["state_update_applied"] = False
            refinement["active_revision"] = int(state["revision"])
            return refinement
        if not apply_state_update:
            if refinement["eligible_for_state_update"]:
                refinement["status"] = "OBSERVATION_ONLY"
                refinement["workflow_complete"] = True
                refinement["eligible_for_state_update"] = False
                refinement["reason"] = (
                    "sample retained for one aggregate multi-sample update"
                )
            refinement["state_update_applied"] = False
            refinement["active_revision"] = int(state["revision"])
            refinement["aggregate_candidate_only"] = True
            refinement["refinement_limits"]["enforced_for_this_sample"] = False
            return refinement
        if refinement["eligible_for_state_update"]:
            try:
                context_revalidation = await self._revalidate_before_update(
                    observation,
                    state,
                )
            except RuntimeError as error:
                refinement["status"] = "REJECTED_CONTEXT_CHANGED"
                refinement["workflow_complete"] = True
                refinement["eligible_for_state_update"] = False
                refinement["state_update_applied"] = False
                refinement["reason"] = str(error)
                return refinement
            refinement["context_revalidation"] = context_revalidation
            updated_state = apply_compact_translation_update(state, refinement)
            swapped = await self.state_store.compare_and_swap(
                expected_revision=int(state["revision"]),
                state=updated_state,
                refinement=refinement,
            )
            if not swapped:
                refinement["status"] = "STALE_ACTIVE_REVISION"
                refinement["workflow_complete"] = True
                refinement["eligible_for_state_update"] = False
                refinement["state_update_applied"] = False
                return refinement
            refinement["state_update_applied"] = True
            refinement["active_revision"] = int(updated_state["revision"])
        else:
            refinement["state_update_applied"] = False
            refinement["active_revision"] = int(state["revision"])
        return refinement

    async def _run_multi_sample(
        self,
        *,
        adoption_factor: float,
        sample_count: int,
        landmark_id: str | None,
        run_id: str,
    ) -> dict[str, Any]:
        source_state = await self.state_store.snapshot()
        landmark = select_visual_landmark(self.profile, landmark_id)
        reference_images = await self._load_reference_images(landmark)
        observations = await self._capture_multi_sample_observations(
            source_state,
            sample_count,
            landmark,
        )
        sample_outcomes = await asyncio.gather(
            *[
                self._run_single(
                    adoption_factor=adoption_factor,
                    landmark_id=landmark_id,
                    apply_state_update=False,
                    enforce_delta_limits=False,
                    state_override=source_state,
                    observation_override=observation,
                    reference_images_override=reference_images,
                    request_id_prefix=f"{run_id}/sample-{index + 1:02d}",
                )
                for index, observation in enumerate(observations)
            ],
            return_exceptions=True,
        )
        sample_results: list[dict[str, Any]] = []
        for outcome in sample_outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
            if not isinstance(outcome, dict):
                raise RuntimeError("multi-sample analysis returned invalid data")
            sample_results.append(outcome)
        indexed_results = list(enumerate(sample_results, start=1))
        accepted_indexed_results = [
            (index, result)
            for index, result in indexed_results
            if result.get("status") == "OBSERVATION_ONLY"
        ]
        excluded_indexed_results = [
            (index, result)
            for index, result in indexed_results
            if result.get("status") != "OBSERVATION_ONLY"
        ]
        accepted_sample_indexes = [
            index for index, _ in accepted_indexed_results
        ]
        excluded_sample_indexes = [
            index for index, _ in excluded_indexed_results
        ]
        if not accepted_indexed_results:
            first_index, first_result = indexed_results[0]
            return self._multi_sample_rejection(
                result=first_result,
                sample_results=sample_results,
                requested_sample_count=sample_count,
                reason=(
                    "multi-sample refinement excluded every sample; "
                    f"sample {first_index}: "
                    + str(
                        first_result.get("reason")
                        or (first_result.get("quality_review") or {}).get(
                            "reason"
                        )
                        or first_result.get("status")
                    )
                ),
                included_sample_indexes=[],
            )
        accepted_results = [
            result for _, result in accepted_indexed_results
        ]
        for index, result in accepted_indexed_results:
            if (
                int(result.get("source_revision", -1))
                != int(source_state["revision"])
                or result.get("identities") != source_state.get("identities")
                or not np.array_equal(
                    np.asarray(result.get("source_world_from_base")),
                    np.asarray(source_state.get("world_from_base")),
                )
            ):
                return self._multi_sample_rejection(
                    result=result,
                    sample_results=sample_results,
                    requested_sample_count=sample_count,
                    reason=(
                        f"accepted sample {index} does not match the frozen "
                        "multi-sample alignment context"
                    ),
                    status="REJECTED_CONTEXT_CHANGED",
                    included_sample_indexes=accepted_sample_indexes,
                )

        raw_deltas = np.asarray(
            [item["raw_translation_delta_m"] for item in accepted_results],
            dtype=np.float64,
        )
        mean_raw_delta = np.mean(raw_deltas, axis=0)
        component_standard_deviation = np.std(raw_deltas, axis=0)
        sample_distances_from_mean = np.linalg.norm(
            raw_deltas - mean_raw_delta,
            axis=1,
        )
        source_transform = rigid_transform(
            source_state["world_from_base"],
            "source_state.world_from_base",
        )
        adopted_delta = adoption_factor * mean_raw_delta
        proposed_transform = source_transform.copy()
        proposed_transform[:3, 3] += adopted_delta
        raw_norm_m = float(np.linalg.norm(mean_raw_delta))
        adopted_norm_m = float(np.linalg.norm(adopted_delta))
        accepted_sample_count = len(accepted_results)
        excluded_sample_count = len(excluded_indexed_results)
        scaled_raw_limit_m = (
            self.maximum_raw_translation_delta_m * accepted_sample_count
        )
        scaled_adopted_limit_m = (
            self.maximum_adopted_translation_delta_m * accepted_sample_count
        )
        delta_limit_exceeded = adoption_factor > 0.0 and (
            raw_norm_m > scaled_raw_limit_m
            or adopted_norm_m > scaled_adopted_limit_m
        )

        aggregate = copy.deepcopy(accepted_results[-1])
        aggregate.pop("aggregate_candidate_only", None)
        aggregate["source_world_from_base"] = source_transform.tolist()
        aggregate["estimated_full_translation_m"] = (
            source_transform[:3, 3] + mean_raw_delta
        ).tolist()
        aggregate["raw_translation_delta_m"] = mean_raw_delta.tolist()
        aggregate["raw_translation_delta_norm_m"] = raw_norm_m
        aggregate["adoption_factor"] = adoption_factor
        aggregate["adopted_translation_delta_m"] = adopted_delta.tolist()
        aggregate["adopted_translation_delta_norm_m"] = adopted_norm_m
        aggregate["proposed_world_from_base"] = proposed_transform.tolist()
        aggregate["observation_provenance"] = {
            "aggregation": "ARITHMETIC_MEAN_OF_RAW_TRANSLATION_DELTAS",
            "samples": [
                copy.deepcopy(item.get("observation_provenance"))
                for item in accepted_results
            ],
            "excluded_sample_indexes": excluded_sample_indexes,
        }
        aggregate["quality_review"] = {
            "required": any(
                bool((item.get("quality_review") or {}).get("required"))
                for item in accepted_results
            ),
            "threshold_m": self.review_threshold_m,
            "threshold_basis": "EACH_RAW_SAMPLE_BEFORE_AGGREGATION",
            "verdict": (
                "PASS"
                if any(
                    bool((item.get("quality_review") or {}).get("required"))
                    for item in accepted_results
                )
                else "NOT_RUN"
            ),
            "reason": (
                "Every required review for an accepted sample passed; "
                "failed samples were excluded from aggregation."
            ),
            "reviewed_point_ids": sorted(
                {
                    str(point_id)
                    for item in accepted_results
                    for point_id in (
                        (item.get("quality_review") or {}).get(
                            "reviewed_point_ids"
                        )
                        or []
                    )
                }
            ),
        }
        aggregate["refinement_limits"] = {
            "threshold_scale": accepted_sample_count,
            "threshold_scale_basis": "ACCEPTED_SAMPLE_COUNT",
            "base_maximum_raw_translation_delta_m": (
                self.maximum_raw_translation_delta_m
            ),
            "base_maximum_adopted_translation_delta_m": (
                self.maximum_adopted_translation_delta_m
            ),
            "maximum_raw_translation_delta_m": scaled_raw_limit_m,
            "maximum_adopted_translation_delta_m": scaled_adopted_limit_m,
            "enforced_on": "ARITHMETIC_MEAN",
        }
        accepted_index_set = set(accepted_sample_indexes)
        sample_summaries = [
            self._sample_summary(
                item,
                index,
                included_in_aggregation=index in accepted_index_set,
            )
            for index, item in indexed_results
        ]
        aggregate["multi_sample_refinement"] = {
            "feature_name": "MULTI_SAMPLE_REFINEMENT",
            "requested_sample_count": sample_count,
            "completed_sample_count": len(sample_results),
            "accepted_sample_count": accepted_sample_count,
            "excluded_sample_count": excluded_sample_count,
            "accepted_sample_indexes": accepted_sample_indexes,
            "excluded_sample_indexes": excluded_sample_indexes,
            "aggregation": "ARITHMETIC_MEAN_OF_RAW_TRANSLATION_DELTAS",
            "aggregation_population": "ACCEPTED_SAMPLES_ONLY",
            "threshold_scale": accepted_sample_count,
            "threshold_scale_basis": "ACCEPTED_SAMPLE_COUNT",
            "capture_execution": "SEQUENTIAL_DISTINCT_RGBD_FRAMES",
            "analysis_execution": "CONCURRENT_PER_SAMPLE",
            "mean_raw_translation_delta_m": mean_raw_delta.tolist(),
            "component_standard_deviation_m": (
                component_standard_deviation.tolist()
            ),
            "maximum_sample_distance_from_mean_m": float(
                np.max(sample_distances_from_mean)
            ),
            "samples": sample_summaries,
        }
        aggregate["sample_visual_evidence"] = [
            copy.deepcopy(item.get("visual_evidence"))
            for item in sample_results
        ]
        aggregate["sample_quality_review_evidence"] = [
            copy.deepcopy(item.get("quality_review_evidence"))
            for item in sample_results
        ]
        aggregate["sample_capture_motion"] = [
            copy.deepcopy(item.get("capture_motion"))
            for item in sample_results
        ]
        aggregate["sample_landmark_depth_reselection"] = [
            copy.deepcopy(item.get("landmark_depth_reselection"))
            for item in sample_results
        ]
        aggregate["visual_evidence_represents"] = (
            "LAST_ACCEPTED_SAMPLE; aggregate XYZ is reported numerically"
        )
        aggregate["workflow_complete"] = True
        aggregate["state_update_applied"] = False
        aggregate["active_revision"] = int(source_state["revision"])

        if adoption_factor == 0.0:
            aggregate["status"] = "OBSERVATION_ONLY"
            aggregate["eligible_for_state_update"] = False
            aggregate["reason"] = (
                f"multi-sample refinement averaged {accepted_sample_count} "
                f"accepted sample(s) and excluded {excluded_sample_count}; "
                "adoption_factor is zero"
            )
            return aggregate
        if delta_limit_exceeded:
            aggregate["status"] = "REJECTED_DELTA_LIMIT"
            aggregate["eligible_for_state_update"] = False
            aggregate["reason"] = (
                "averaged raw translation delta exceeds the scaled "
                "accepted-sample limit"
                if raw_norm_m > scaled_raw_limit_m
                else (
                    "averaged adopted translation delta exceeds the scaled "
                    "accepted-sample limit; reduce adoption_factor"
                )
            )
            return aggregate

        current_state = await self.state_store.snapshot()
        if (
            int(current_state.get("revision", -1))
            != int(source_state["revision"])
            or current_state.get("identities") != source_state.get("identities")
            or not np.array_equal(
                np.asarray(current_state.get("world_from_base")),
                source_transform,
            )
        ):
            aggregate["status"] = "REJECTED_CONTEXT_CHANGED"
            aggregate["eligible_for_state_update"] = False
            aggregate["reason"] = (
                "active alignment changed before the multi-sample update"
            )
            return aggregate
        try:
            context_revalidation = await self._revalidate_before_update(
                {
                    "provenance": copy.deepcopy(
                        accepted_results[-1].get("observation_provenance")
                    )
                },
                source_state,
            )
        except RuntimeError as error:
            aggregate["status"] = "REJECTED_CONTEXT_CHANGED"
            aggregate["eligible_for_state_update"] = False
            aggregate["reason"] = str(error)
            return aggregate
        aggregate["status"] = "TRANSLATION_UPDATE_READY"
        aggregate["eligible_for_state_update"] = True
        aggregate["reason"] = (
            f"averaged {accepted_sample_count} accepted sample(s); "
            f"excluded {excluded_sample_count} failed sample(s)"
        )
        aggregate["context_revalidation"] = context_revalidation
        updated_state = apply_compact_translation_update(source_state, aggregate)
        swapped = await self.state_store.compare_and_swap(
            expected_revision=int(source_state["revision"]),
            state=updated_state,
            refinement=aggregate,
        )
        if not swapped:
            aggregate["status"] = "STALE_ACTIVE_REVISION"
            aggregate["eligible_for_state_update"] = False
            aggregate["state_update_applied"] = False
            return aggregate
        aggregate["state_update_applied"] = True
        aggregate["active_revision"] = int(updated_state["revision"])
        return aggregate

    def _attach_single_sample_metadata(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        included = result.get("raw_translation_delta_m") is not None and (
            result.get("status")
            not in {
                "REJECTED_OBSERVATION",
                "REJECTED_QUALITY_REVIEW",
                "DEPENDENCY_UNAVAILABLE",
            }
        )
        result["multi_sample_refinement"] = {
            "feature_name": "MULTI_SAMPLE_REFINEMENT",
            "requested_sample_count": 1,
            "completed_sample_count": 1,
            "accepted_sample_count": int(included),
            "excluded_sample_count": int(not included),
            "accepted_sample_indexes": [1] if included else [],
            "excluded_sample_indexes": [] if included else [1],
            "aggregation": "SINGLE_SAMPLE",
            "aggregation_population": (
                "ACCEPTED_SAMPLES_ONLY" if included else "NO_ACCEPTED_SAMPLES"
            ),
            "threshold_scale": 1,
            "threshold_scale_basis": "SINGLE_SAMPLE",
            "samples": [
                self._sample_summary(
                    result,
                    1,
                    included_in_aggregation=included,
                )
            ],
        }
        return result

    def _multi_sample_rejection(
        self,
        *,
        result: dict[str, Any],
        sample_results: list[dict[str, Any]],
        requested_sample_count: int,
        reason: str,
        status: str | None = None,
        included_sample_indexes: list[int] | None = None,
    ) -> dict[str, Any]:
        included = set(included_sample_indexes or [])
        excluded = [
            index
            for index in range(1, len(sample_results) + 1)
            if index not in included
        ]
        rejected = copy.deepcopy(result)
        rejected["status"] = status or str(
            result.get("status") or "REJECTED_OBSERVATION"
        )
        rejected["workflow_complete"] = True
        rejected["eligible_for_state_update"] = False
        rejected["state_update_applied"] = False
        rejected["reason"] = reason
        rejected["multi_sample_refinement"] = {
            "feature_name": "MULTI_SAMPLE_REFINEMENT",
            "requested_sample_count": requested_sample_count,
            "completed_sample_count": len(sample_results),
            "accepted_sample_count": len(included),
            "excluded_sample_count": len(excluded),
            "accepted_sample_indexes": sorted(included),
            "excluded_sample_indexes": excluded,
            "aggregation": "INCOMPLETE_NO_UPDATE",
            "aggregation_population": (
                "ACCEPTED_SAMPLES_ONLY" if included else "NO_ACCEPTED_SAMPLES"
            ),
            "threshold_scale": len(included),
            "threshold_scale_basis": "ACCEPTED_SAMPLE_COUNT",
            "capture_execution": "SEQUENTIAL_DISTINCT_RGBD_FRAMES",
            "analysis_execution": "CONCURRENT_PER_SAMPLE",
            "samples": [
                self._sample_summary(
                    item,
                    index + 1,
                    included_in_aggregation=(index + 1) in included,
                )
                for index, item in enumerate(sample_results)
            ],
        }
        rejected["sample_visual_evidence"] = [
            copy.deepcopy(item.get("visual_evidence"))
            for item in sample_results
        ]
        rejected["sample_quality_review_evidence"] = [
            copy.deepcopy(item.get("quality_review_evidence"))
            for item in sample_results
        ]
        rejected["sample_capture_motion"] = [
            copy.deepcopy(item.get("capture_motion"))
            for item in sample_results
        ]
        rejected["sample_landmark_depth_reselection"] = [
            copy.deepcopy(item.get("landmark_depth_reselection"))
            for item in sample_results
        ]
        return rejected

    @staticmethod
    def _sample_summary(
        result: dict[str, Any],
        sample_index: int,
        *,
        included_in_aggregation: bool,
    ) -> dict[str, Any]:
        return {
            "sample_index": sample_index,
            "status": result.get("status"),
            "included_in_aggregation": included_in_aggregation,
            "exclusion_reason": (
                None
                if included_in_aggregation
                else str(
                    result.get("reason")
                    or (result.get("quality_review") or {}).get("reason")
                    or result.get("status")
                    or "unknown"
                )
            ),
            "raw_translation_delta_m": copy.deepcopy(
                result.get("raw_translation_delta_m")
            ),
            "raw_translation_delta_norm_m": result.get(
                "raw_translation_delta_norm_m"
            ),
            "quality_review": copy.deepcopy(result.get("quality_review")),
            "landmark_depth_reselection": copy.deepcopy(
                result.get("landmark_depth_reselection")
            ),
            "capture_motion": copy.deepcopy(result.get("capture_motion")),
            "observation_provenance": copy.deepcopy(
                result.get("observation_provenance")
            ),
            "visual_evidence": copy.deepcopy(result.get("visual_evidence")),
            "vlm_invocations": copy.deepcopy(result.get("vlm_invocations")),
        }

    async def _capture_multi_sample_observations(
        self,
        state: dict[str, Any],
        sample_count: int,
        landmark: dict[str, Any],
    ) -> list[dict[str, Any]]:
        policy = self.profile["capture_motion_policy"]
        maximum_wait_s = float(policy["maximum_transform_wait_ms"]) / 1000.0
        retry_interval_s = float(policy["transform_retry_interval_ms"]) / 1000.0
        observations: list[dict[str, Any]] = []
        capture_identities: set[tuple[Any, ...]] = set()
        loop = asyncio.get_running_loop()
        for sample_index in range(sample_count):
            deadline = loop.time() + maximum_wait_s
            while True:
                observation = await self.observation_source()
                self._validate_capture_observation(observation, state)
                tool_point = resolve_tool_landmark_point(
                    landmark,
                    runtime_bindings=observation.get("runtime_landmark_bindings"),
                )
                self._validate_capture_motion(observation, tool_point=tool_point)
                identity = self._capture_identity(observation)
                if identity not in capture_identities:
                    capture_identities.add(identity)
                    observations.append(observation)
                    break
                remaining_s = deadline - loop.time()
                if remaining_s <= 0.0:
                    raise RuntimeError(
                        "multi-sample refinement could not capture a distinct RGB-D "
                        f"frame for sample {sample_index + 1}"
                    )
                await asyncio.sleep(min(retry_interval_s, remaining_s))
        return observations

    @staticmethod
    def _capture_identity(observation: dict[str, Any]) -> tuple[Any, ...]:
        provenance = observation.get("provenance") or {}
        identity = (
            provenance.get("frame_number"),
            provenance.get("observed_at_us"),
            provenance.get("rgb_sha256"),
            provenance.get("registered_depth_sha256"),
        )
        if all(value is None for value in identity):
            raise RuntimeError(
                "multi-sample refinement requires RGB-D frame provenance"
            )
        return identity

    @staticmethod
    def _validate_adoption_factor(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("adoption_factor must be a number from zero to one")
        try:
            factor = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "adoption_factor must be a number from zero to one"
            ) from error
        if not np.isfinite(factor) or not 0.0 <= factor <= 1.0:
            raise ValueError("adoption_factor must be a number from zero to one")
        return factor

    @staticmethod
    def _validate_sample_count(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("sample_count must be an integer from one to five")
        if value < 1 or value > 5:
            raise ValueError("sample_count must be an integer from one to five")
        return value

    async def _invoke_vlm(
        self,
        *,
        prompt: str,
        images: list[dict[str, Any]],
        purpose: str,
        request_id: str,
        invocation_records: list[dict[str, Any]],
    ) -> str:
        try:
            result = await self.vlm.invoke(
                prompt=prompt,
                images=images,
                purpose=purpose,
                request_id=request_id,
            )
            if isinstance(result, str):
                text = result
                route = None
            elif isinstance(result, dict) and isinstance(result.get("text"), str):
                text = str(result["text"])
                route = result.get("route")
            else:
                raise RuntimeError("VLM client returned invalid inference data")
            invocation_records.append(
                {
                    "request_id": request_id,
                    "purpose": purpose,
                    "route": (
                        copy.deepcopy(route) if isinstance(route, dict) else None
                    ),
                }
            )
            return text
        except Exception as error:
            invocation_records.append(
                {
                    "request_id": request_id,
                    "purpose": purpose,
                    "route": None,
                    "error": str(error),
                }
            )
            raise RuntimeError(f"VLM call {purpose} failed: {error}") from error

    @staticmethod
    def _attach_vlm_invocations(
        result: dict[str, Any],
        invocation_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result["vlm_invocations"] = invocation_records
        return result

    async def _load_reference_images(
        self,
        landmark: dict[str, Any],
    ) -> list[dict[str, Any]]:
        asset_ids = [
            str(item)
            for item in (landmark.get("vlm_reference_asset_ids") or [])
        ]
        if not asset_ids:
            return []
        if self.reference_image_source is None:
            raise RuntimeError(
                f"landmark {landmark['landmark_id']} requires configured "
                "VLM reference-image loading"
            )
        images = await self.reference_image_source(asset_ids)
        if not isinstance(images, list) or len(images) != len(asset_ids):
            raise RuntimeError(
                "VLM reference-image source did not resolve every profile asset"
            )
        normalized: list[dict[str, Any]] = []
        for image in images:
            if not isinstance(image, dict):
                raise RuntimeError("VLM reference-image source returned invalid data")
            try:
                normalized.append(self._vlm_image(image))
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "VLM reference-image source returned an invalid image"
                ) from error
        return normalized

    @staticmethod
    def _vlm_image(channel: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(channel["id"]),
            "label": str(channel["label"]),
            "image_bytes": bytes(channel["image_bytes"]),
            "media_type": str(channel["media_type"]),
            "width": int(channel["width"]),
            "height": int(channel["height"]),
        }

    @staticmethod
    def _marked_overlap_channel(
        image_bytes: bytes,
        depth_grid: tuple[int, ...],
    ) -> dict[str, Any]:
        return {
            "id": "marked_overlap",
            "label": "VLM Landmark Marking",
            "image_bytes": image_bytes,
            "media_type": "image/png",
            "width": int(depth_grid[1]),
            "height": int(depth_grid[0]),
        }

    async def _publish_evidence(
        self,
        *,
        channels: list[dict[str, Any]],
        annotations: list[dict[str, Any]],
        landmark: dict[str, Any],
        confidence: str,
    ) -> dict[str, Any]:
        return await self.visual_evidence_publisher.register_channels(
            channels=channels,
            default_channel="marked_overlap",
            title=(
                "Arm-root translation refinement: "
                + str(landmark["display_name"])
            ),
            annotations=annotations,
            confidence=confidence,
            model=str(self.vlm.model_id),
            source_skill="refine_arm_root_translation",
        )

    async def _revalidate_before_update(
        self,
        observation: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        current = await self.state_revalidator(observation)
        if not isinstance(current, dict):
            raise RuntimeError("arm-root refinement revalidation returned invalid data")
        if current.get("tracking_state") != "TRACKING":
            raise RuntimeError(
                "world tracking is not TRACKING after landmark VLM inference"
            )
        if current.get("identities") != state.get("identities"):
            raise RuntimeError(
                "arm-root translation refinement identities changed during VLM inference"
            )
        return {
            "capture_context_unchanged": True,
            "checked_at_us": current.get("checked_at_us"),
            "post_capture_tool_motion_invalidates_capture": False,
        }

    def _validate_capture_observation(
        self,
        observation: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        if not isinstance(observation, dict):
            raise ValueError("observation source must return an object")
        if observation.get("coherent_snapshot") is not True:
            raise RuntimeError(
                "translation refinement requires one coherent RGB-D/FK snapshot"
            )
        if observation.get("tracking_state") != "TRACKING":
            raise RuntimeError("translation refinement requires TRACKING world state")
        for field in (
            "rgb",
            "registered_depth_m",
            "intrinsics",
            "world_from_camera",
            "base_from_tool",
            "provenance",
        ):
            if field not in observation:
                raise ValueError(f"capture observation is missing {field}")
        if observation.get("identities") != state.get("identities"):
            raise RuntimeError(
                "capture observation identities do not match active state"
            )
        identities = state.get("identities") or {}
        for field in (
            "world_frame",
            "vio_session_epoch",
            "spatial_convention",
            "camera_provider_id",
            "camera_boot_id",
            "camera_calibration_revision",
            "arm_provider_id",
            "arm_boot_id",
        ):
            if not str(identities.get(field) or "").strip():
                raise RuntimeError(f"active alignment identity {field} is missing")
        compatibility = self.profile["robot_compatibility"]
        expected_identities = {
            "arm_model_id": compatibility["model_id"],
            "arm_model_revision": compatibility["model_revision"],
            "effector_profile_revision": self.profile["profile_revision"],
        }
        for field, expected in expected_identities.items():
            if identities.get(field) != expected:
                raise RuntimeError(
                    f"active {field} does not match the effector profile"
                )
        if not isinstance(observation.get("provenance"), dict):
            raise ValueError("capture observation provenance must be an object")

    def _validate_capture_motion(
        self,
        observation: dict[str, Any],
        *,
        tool_point: np.ndarray,
    ) -> dict[str, Any]:
        temporal = observation.get("temporal_alignment")
        if not isinstance(temporal, dict):
            raise RuntimeError(
                "translation refinement requires timestamped RGB-D/FK alignment"
            )
        if temporal.get("policy_id") != "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1":
            raise RuntimeError("translation refinement temporal policy is unsupported")
        samples = temporal.get("base_from_tool_samples")
        if not isinstance(samples, list) or len(samples) < 3:
            raise RuntimeError(
                "translation refinement requires at least three timestamped FK samples"
            )
        landmark_positions: list[np.ndarray] = []
        sample_times: list[int] = []
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise RuntimeError("timestamped FK sample is invalid")
            at_us = sample.get("at_us")
            if not isinstance(at_us, int) or at_us <= 0:
                raise RuntimeError("timestamped FK sample has no valid timestamp")
            if int(sample.get("maximum_extrapolation_us") or 0) != 0:
                raise RuntimeError(
                    "timestamped FK sample is extrapolated instead of bracketed"
                )
            matrix = rigid_transform(
                sample.get("base_from_tool"),
                f"temporal_alignment.base_from_tool_samples[{index}]",
            )
            homogeneous = np.ones(4, dtype=np.float64)
            homogeneous[:3] = tool_point
            landmark_positions.append((matrix @ homogeneous)[:3])
            sample_times.append(at_us)
        if sample_times != sorted(sample_times) or len(set(sample_times)) != len(
            sample_times
        ):
            raise RuntimeError("timestamped FK samples are not strictly ordered")
        maximum_motion_m = 0.0
        for left in range(len(landmark_positions)):
            for right in range(left + 1, len(landmark_positions)):
                maximum_motion_m = max(
                    maximum_motion_m,
                    float(
                        np.linalg.norm(
                            landmark_positions[right] - landmark_positions[left]
                        )
                    ),
                )
        if maximum_motion_m > self.maximum_capture_landmark_motion_m:
            raise RuntimeError(
                "effector landmark moved more than the capture-time limit: "
                f"{maximum_motion_m:.6f} m > "
                f"{self.maximum_capture_landmark_motion_m:.6f} m"
            )
        return {
            "policy_id": "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1",
            "sample_count": len(samples),
            "window_start_us": sample_times[0],
            "window_end_us": sample_times[-1],
            "measured_maximum_landmark_motion_m": maximum_motion_m,
            "maximum_allowed_landmark_motion_m": (
                self.maximum_capture_landmark_motion_m
            ),
            "fk_extrapolation_allowed": False,
        }
