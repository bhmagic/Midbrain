from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from observe_pointed_object import (
    parse_item_landmark_vlm_result,
    resolve_item_location,
)

from .phase4_policy import report_operation_progress
from .spatial_registration_adapter import (
    CAMERA_OPTICAL_CONVENTION_ID,
    WORLD_CONVENTION_ID,
    SpatialRegistrationSkillAdapter,
)
from .vlm_router import VisionLanguageRouter
from .visual_evidence import VisualEvidenceStore


ITEM_LOCATOR_TEMPORAL_POLICY_ID = "observe-pointed-object.input-time.v2"
ITEM_VISUAL_SUPPORT_POLICY_ID = "item-box.visual-separation.v1"
MINIMUM_ITEM_BOX_CONTRAST = 0.08
ITEM_VISUAL_CHANNEL_IDS = ("rgb", "depth", "rgb_depth")


class MetricItemLocatorAdapter:
    """Locate a visible item with metric or explicitly degraded evidence."""

    def __init__(
        self,
        spatial: SpatialRegistrationSkillAdapter,
        router: VisionLanguageRouter,
        *,
        maximum_source_age_at_completion_ms: float = 60_000.0,
        evidence_dir: Path | None = None,
        semantic_assertion_publisher: Any | None = None,
        visual_evidence_store: VisualEvidenceStore | None = None,
    ):
        if float(maximum_source_age_at_completion_ms) <= 0.0:
            raise ValueError(
                "maximum_source_age_at_completion_ms must be positive"
            )
        self.spatial = spatial
        self.router = router
        self.maximum_source_age_at_completion_ms = float(
            maximum_source_age_at_completion_ms
        )
        self.evidence_dir = (
            evidence_dir.resolve() if evidence_dir is not None else None
        )
        self.last_result: dict[str, Any] | None = None
        self.last_metric_result: dict[str, Any] | None = None
        self.semantic_assertion_publisher = semantic_assertion_publisher
        self.visual_evidence_store = visual_evidence_store

    async def run(
        self,
        *,
        question: str,
        target_frame: str,
        object_id: str | None = None,
        contact_policy: str = "WORKPIECE_CONTACT_ALLOWED",
        depth_requirement: str = "PREFER_METRIC",
        task_plane: dict[str, Any] | None = None,
        spatial_context: Any | None = None,
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be non-empty")
        if not isinstance(target_frame, str) or not target_frame.strip():
            raise ValueError("target_frame must be non-empty")
        requested_target = target_frame.strip()
        skill_id = f"locate-item-{uuid4()}"
        context = spatial_context
        if context is None:
            context = await self.spatial.prepare_context(
                target_frame=requested_target,
                skill_id=skill_id,
            )
        elif str(getattr(context, "target_frame", "")) != requested_target:
            raise ValueError("shared spatial context target frame changed")
        frame = context.frame

        report_operation_progress("BUILD_ITEM_LOCATOR_RGB_EVIDENCE")
        image_bytes, evidence = build_item_locator_evidence(
            frame.rgb,
            frame.depth_m,
        )
        evidence_image: dict[str, Any] | None = None
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_path = self.evidence_dir / f"{skill_id}-evidence.png"
            evidence_path.write_bytes(image_bytes)
            evidence_image = {
                "path": str(evidence_path),
                "mime_type": "image/png",
                "purpose": "SINGLE_RGB_ITEM_LOCALIZATION_EVIDENCE",
            }

        report_operation_progress("VLM_LOCATE_ITEM")
        inference = await self.router.generate(
            image_bytes=image_bytes,
            mime_type="image/png",
            prompt=_item_locator_prompt(
                question=question.strip(),
                depth_height=int(frame.depth_m.shape[0]),
                depth_width=int(frame.depth_m.shape[1]),
            ),
        )
        vlm_result = parse_item_landmark_vlm_result(
            inference.text,
            registered_depth_grid=tuple(
                int(value) for value in frame.depth_m.shape
            ),
        )
        visual_box_support = evaluate_item_visual_support(
            frame.rgb,
            frame.depth_m,
            vlm_result=vlm_result,
        )

        report_operation_progress("REVALIDATE_ITEM_LOCATOR_CAMERA_BINDING")
        current_binding = await self.spatial.revalidate_context_binding(
            context
        )
        completed_at_us = time.time_ns() // 1000
        source_age_ms = max(
            0.0,
            (completed_at_us - int(frame.timestamp_us)) / 1000.0,
        )
        if source_age_ms > self.maximum_source_age_at_completion_ms:
            raise RuntimeError(
                "SOURCE_TOO_OLD_AFTER_VLM: item-locator source age "
                f"{source_age_ms:.1f} ms exceeds "
                f"{self.maximum_source_age_at_completion_ms:.1f} ms"
            )

        report_operation_progress("RESOLVE_ITEM_LOCATION")
        resolved = resolve_item_location(
            vlm_result={
                **vlm_result,
                "backend_id": inference.backend_id,
                "model": inference.model_id,
                "request_id": skill_id,
            },
            registered_depth_m=frame.depth_m,
            intrinsics=dict(frame.intrinsics),
            target_from_camera=context.target_from_camera,
            observed_at_us=int(frame.timestamp_us),
            source_frame=str(frame.camera_frame),
            target_frame=context.target_frame,
            calibration_revision=frame.calibration_revision,
            route_provenance=context.selection.as_dict(),
            object_id=object_id,
            contact_policy=contact_policy,
            depth_requirement=depth_requirement,
            task_plane=task_plane,
            valid_region=context.valid_region,
        )
        if (
            visual_box_support["decision"] == "REJECT"
            and resolved.get("eligible_for_control_math") is True
        ):
            rejected_depth = resolved.get("depth_evidence")
            resolved.update(
                {
                    "status": "REJECTED_OBSERVATION",
                    "eligible_for_control_math": False,
                    "motion_usable": False,
                    "metric_source": "NONE",
                    "location": None,
                    "depth_evidence": None,
                    "rejected_depth_evidence": rejected_depth,
                    "volume_hint": None,
                    "degraded_reason": "VLM_BOX_VISUAL_SUPPORT_REJECTED",
                    "recommended_next_action": (
                        "REOBSERVE_FULL_ITEM_SILHOUETTE"
                    ),
                    "quality_reasons": [
                        *list(resolved.get("quality_reasons") or []),
                        "VLM_BOX_LACKS_VISUAL_SEPARATION",
                    ],
                }
            )
        visual_evidence = None
        if self.visual_evidence_store is not None:
            annotations = _item_annotations(
                vlm_result=vlm_result,
                resolved=resolved,
                evidence=evidence,
            )
            image_height, image_width = evidence["image_grid"]
            visual_channels = build_item_locator_visual_channels(
                frame.rgb,
                frame.depth_m,
            )
            visual_evidence = await self.visual_evidence_store.register_channels(
                channels=visual_channels,
                default_channel="rgb",
                title=(
                    "Metric item localization: "
                    + str(resolved.get("item_label") or "visible item")
                ),
                annotations=annotations,
                confidence=(
                    _visual_confidence(vlm_result.get("confidence"))
                    if visual_box_support["decision"] == "ACCEPT"
                    else "low"
                ),
                model=inference.model_id,
                source_skill="locate_item",
            )
        result = {
            **resolved,
            "skill_id": skill_id,
            "safety_class": "READ_ONLY",
            "physical_action_submitted": False,
            "control_frame_published": False,
            "capability_binding": current_binding.as_dict(),
            "binding_mode": self.spatial.binding_mode,
            "generic_route_mode": self.spatial.generic_route_mode,
            "camera_capture": self.spatial.capture_provenance(context),
            "transform_provenance": self.spatial.transform_provenance(
                context
            ),
            "selected_route_metadata": self.spatial.route_metadata(context),
            "source_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
            "target_convention_id": WORLD_CONVENTION_ID,
            "vlm_route": inference.as_dict(),
            "vlm_geometry": {
                "source_coordinate_space": vlm_result.get(
                    "source_coordinate_space"
                ),
                "source_pixel_yx": vlm_result.get(
                    "source_registered_depth_pixel_yx"
                ),
                "source_box_yxyx": vlm_result.get(
                    "source_registered_depth_box_yxyx"
                ),
                "registered_depth_pixel_yx": vlm_result.get(
                    "registered_depth_pixel_yx"
                ),
                "registered_depth_box_yxyx": vlm_result.get(
                    "registered_depth_box_yxyx"
                ),
                "conversion": vlm_result.get("coordinate_conversion"),
            },
            "vlm_evidence": evidence,
            "visual_box_support": visual_box_support,
            "evidence_image": evidence_image,
            "visual_evidence": visual_evidence,
            "input_temporal_evidence": {
                "policy_id": ITEM_LOCATOR_TEMPORAL_POLICY_ID,
                "source_observed_at_us": int(frame.timestamp_us),
                "skill_completed_at_us": completed_at_us,
                "source_age_at_completion_ms": source_age_ms,
                "maximum_source_age_at_completion_ms": (
                    self.maximum_source_age_at_completion_ms
                ),
                "spatial_inputs": dict(context.temporal_evidence),
            },
        }
        result["semantic_scene_assertion"] = {
            "status": "NOT_CONFIGURED"
        }
        if (
            result.get("eligible_for_control_math") is True
            and self.semantic_assertion_publisher is not None
        ):
            try:
                result["semantic_scene_assertion"] = (
                    await self.semantic_assertion_publisher.publish_item_location(
                        result
                    )
                )
            except Exception as error:
                result["semantic_scene_assertion"] = {
                    "status": "PUBLISH_FAILED",
                    "error": str(error),
                    "location_result_preserved": True,
                }
        self.last_result = result
        if result.get("eligible_for_control_math") is True:
            self.last_metric_result = result
        return result


def _visual_confidence(value: Any) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.60:
        return "medium"
    return "low"


def build_item_locator_evidence(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
) -> tuple[bytes, dict[str, Any]]:
    """Build one clean RGB image on the exact registered-depth grid."""

    color = np.asarray(rgb)
    depth = np.asarray(registered_depth_m)
    rgb_on_depth = _rgb_on_depth_grid(color, depth)
    depth_height, depth_width = (int(value) for value in depth.shape)
    ok, encoded = cv2.imencode(
        ".png",
        cv2.cvtColor(rgb_on_depth, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_PNG_COMPRESSION, 3],
    )
    if not ok:
        raise RuntimeError("could not encode item-locator RGB evidence")
    return encoded.tobytes(), {
        "image_layout": "SINGLE_RGB_ON_REGISTERED_DEPTH_GRID",
        "image_grid": [depth_height, depth_width],
        "rgb_source_grid": [int(color.shape[0]), int(color.shape[1])],
        "registered_depth_grid": [depth_height, depth_width],
        "rgb_resampled_to_registered_depth_grid": (
            color.shape[:2] != depth.shape
        ),
        "coordinate_contract": "ORIGINAL_REGISTERED_DEPTH_PIXEL_YX",
        "lossless_encoding": "PNG",
        "depth_not_shown_to_vlm": True,
    }


def build_item_locator_visual_channels(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
) -> list[dict[str, Any]]:
    """Build co-registered RGB, depth, and overlay viewer channels."""

    color = _rgb_on_depth_grid(rgb, registered_depth_m)
    depth = np.asarray(registered_depth_m, dtype=np.float32)
    valid = np.isfinite(depth) & (depth > 0.0)
    depth_color = np.zeros_like(color, dtype=np.uint8)
    if np.any(valid):
        values = depth[valid]
        near = float(np.percentile(values, 2.0))
        far = float(np.percentile(values, 98.0))
        if far <= near + 1e-6:
            far = near + 1e-6
        scaled = np.clip((depth - near) / (far - near), 0.0, 1.0)
        depth_u8 = np.asarray(np.round((1.0 - scaled) * 255.0), dtype=np.uint8)
        depth_bgr = cv2.applyColorMap(depth_u8, cv2.COLORMAP_TURBO)
        depth_color = cv2.cvtColor(depth_bgr, cv2.COLOR_BGR2RGB)
        depth_color[~valid] = 0
    overlay = cv2.addWeighted(color, 0.62, depth_color, 0.38, 0.0)
    overlay[~valid] = color[~valid]
    height, width = depth.shape

    def channel(
        channel_id: str,
        label: str,
        image: np.ndarray,
    ) -> dict[str, Any]:
        ok, encoded = cv2.imencode(
            ".png",
            cv2.cvtColor(image, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_PNG_COMPRESSION, 3],
        )
        if not ok:
            raise RuntimeError(
                f"could not encode item-locator {channel_id} evidence"
            )
        return {
            "id": channel_id,
            "label": label,
            "image_bytes": encoded.tobytes(),
            "media_type": "image/png",
            "width": int(width),
            "height": int(height),
        }

    return [
        channel("rgb", "RGB", color),
        channel("depth", "Registered Depth", depth_color),
        channel("rgb_depth", "RGB + Depth", overlay),
    ]


def _rgb_on_depth_grid(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
) -> np.ndarray:
    color = np.asarray(rgb)
    depth = np.asarray(registered_depth_m)
    if color.ndim != 3 or color.shape[2] != 3:
        raise ValueError("rgb must be an HxWx3 array")
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    depth_height, depth_width = (int(value) for value in depth.shape)
    if color.shape[:2] == depth.shape:
        return np.ascontiguousarray(color, dtype=np.uint8)
    return cv2.resize(
        np.asarray(color, dtype=np.uint8),
        (depth_width, depth_height),
        interpolation=cv2.INTER_AREA,
    )


def evaluate_item_visual_support(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
    *,
    vlm_result: dict[str, Any],
    minimum_contrast: float = MINIMUM_ITEM_BOX_CONTRAST,
) -> dict[str, Any]:
    """Reject a box that looks like an undifferentiated support fragment."""

    image = _rgb_on_depth_grid(rgb, registered_depth_m)
    raw_box = vlm_result.get("registered_depth_box_yxyx")
    raw_pixel = vlm_result.get("registered_depth_pixel_yx")
    if raw_box is None or raw_pixel is None:
        return {
            "policy_id": ITEM_VISUAL_SUPPORT_POLICY_ID,
            "decision": "NOT_APPLICABLE",
            "reason": "VLM_SCENE_UNSUITABLE",
        }
    y0, x0, y1, x1 = (int(value) for value in raw_box)
    y, x = (int(value) for value in raw_pixel)
    box_height = y1 - y0
    box_width = x1 - x0
    padding = max(8, int(round(max(box_height, box_width) * 0.25)))
    ring_y0 = max(0, y0 - padding)
    ring_x0 = max(0, x0 - padding)
    ring_y1 = min(image.shape[0], y1 + padding)
    ring_x1 = min(image.shape[1], x1 + padding)
    region = image[ring_y0:ring_y1, ring_x0:ring_x1]
    ring_mask = np.ones(region.shape[:2], dtype=bool)
    ring_mask[
        y0 - ring_y0 : y1 - ring_y0,
        x0 - ring_x0 : x1 - ring_x0,
    ] = False
    inside = image[y0:y1, x0:x1].reshape(-1, 3).astype(np.float64)
    ring = region[ring_mask].reshape(-1, 3).astype(np.float64)
    local = image[
        max(0, y - 4) : min(image.shape[0], y + 5),
        max(0, x - 4) : min(image.shape[1], x + 5),
    ].reshape(-1, 3).astype(np.float64)
    if not inside.size or not ring.size or not local.size:
        return {
            "policy_id": ITEM_VISUAL_SUPPORT_POLICY_ID,
            "decision": "REJECT",
            "reason": "INSUFFICIENT_BOX_OR_SURROUND_PIXELS",
        }

    normalization = 255.0 * float(np.sqrt(3.0))
    ring_median = np.median(ring, axis=0)
    box_contrast = float(
        np.linalg.norm(np.median(inside, axis=0) - ring_median)
        / normalization
    )
    selected_contrast = float(
        np.linalg.norm(np.median(local, axis=0) - ring_median)
        / normalization
    )
    score = max(box_contrast, selected_contrast)
    threshold = float(minimum_contrast)
    decision = "ACCEPT" if score >= threshold else "REJECT"
    return {
        "policy_id": ITEM_VISUAL_SUPPORT_POLICY_ID,
        "decision": decision,
        "reason": (
            "ITEM_SEPARATES_FROM_LOCAL_SURROUND"
            if decision == "ACCEPT"
            else "BOX_MATCHES_SUPPORT_OR_BACKGROUND"
        ),
        "box_yxyx": [y0, x0, y1, x1],
        "selected_pixel_yx": [y, x],
        "box_width_px": box_width,
        "box_height_px": box_height,
        "ring_padding_px": padding,
        "box_ring_contrast": box_contrast,
        "selected_ring_contrast": selected_contrast,
        "contrast_score": score,
        "minimum_contrast": threshold,
    }


def _item_annotations(
    *,
    vlm_result: dict[str, Any],
    resolved: dict[str, Any],
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Annotate one item selection once on the clean RGB evidence."""

    depth_height, depth_width = (
        int(value) for value in evidence["registered_depth_grid"]
    )
    item_label = str(resolved.get("item_label") or "item")
    confidence = _visual_confidence(vlm_result.get("confidence"))

    def point(pixel_yx: list[int], suffix: str, label: str) -> dict[str, Any]:
        y, x = (int(value) for value in pixel_yx)
        return {
            "id": f"item-{suffix}",
            "type": "point",
            "label": label,
            "confidence": confidence,
            "applies_to_channels": list(ITEM_VISUAL_CHANNEL_IDS),
            "x": (x + 0.5) / depth_width,
            "y": (y + 0.5) / depth_height,
        }

    def box(box_yxyx: list[int]) -> dict[str, Any]:
        y0, x0, y1, x1 = (int(value) for value in box_yxyx)
        return {
            "id": "item-box",
            "type": "box",
            "label": item_label,
            "confidence": confidence,
            "applies_to_channels": list(ITEM_VISUAL_CHANNEL_IDS),
            "x": x0 / depth_width,
            "y": y0 / depth_height,
            "width": (x1 - x0) / depth_width,
            "height": (y1 - y0) / depth_height,
        }

    raw_pixel = vlm_result.get("registered_depth_pixel_yx")
    raw_box = vlm_result.get("registered_depth_box_yxyx")
    if (
        not isinstance(raw_pixel, list)
        or len(raw_pixel) != 2
        or not isinstance(raw_box, list)
        or len(raw_box) != 4
    ):
        return []
    requested_pixel = list(raw_pixel)
    requested_box = list(raw_box)
    annotations = [
        box(requested_box),
        point(requested_pixel, "requested", f"{item_label} selection"),
    ]

    depth_evidence = resolved.get("depth_evidence")
    selected_pixel = (
        depth_evidence.get("pixel_yx")
        if isinstance(depth_evidence, dict)
        else None
    )
    if (
        isinstance(selected_pixel, list)
        and len(selected_pixel) == 2
        and list(selected_pixel) != requested_pixel
    ):
        annotations.append(
            point(
                list(selected_pixel),
                "metric-depth",
                f"{item_label} metric depth sample",
            )
        )
    return annotations

def _item_locator_prompt(
        *,
        question: str,
        depth_height: int,
        depth_width: int,
) -> str:
    return f"""
Locate the one visible item requested by the user.

User request: {question}

The image is one clean RGB view resampled onto the registered-depth grid. Depth
is intentionally not shown because the deterministic host, not the model,
decides whether depth belongs to the selected surface. Return every point and
box coordinate as an integer in NORMALIZED_0_1000 image space, independent of
the image's displayed or encoded resolution. Coordinates use [y, x], origin
top-left, +X right, +Y down. The deterministic host will map that normalized
space onto the registered-depth grid of height={depth_height},
width={depth_width}.

Select a representative visible point on the requested item, preferably near
its visible center and away from boundaries or holes. The box MUST enclose the
complete visible silhouette of the requested item, including its full width and
height. Do not return a narrow edge, texture strip, highlight, shadow, hole,
support surface, or background fragment. If the complete visible item cannot be
boxed confidently, reject the scene. Classify visibly shiny/mirror-like items
as REFLECTIVE, glass or
clear plastic as TRANSPARENT, and narrow wire/mesh/perforated geometry as
THIN_OR_PERFORATED. Never claim that a depth value belongs to the item; the
deterministic resolver checks depth separately. Allow same-surface search only
when a nearby point inside the tight item box should describe the same rigid
item surface. If the target is ambiguous or not visible, reject the scene.

Return only one JSON object with exactly this schema:
{{
  "schema": "physical_agent.item_landmark_vlm",
  "schema_version": 2,
  "coordinate_space": "NORMALIZED_0_1000",
  "scene_suitable": true,
  "reason": "brief visible evidence",
  "item_label": "concise item label",
  "confidence": 0.0,
  "material_class": "OPAQUE_DIFFUSE|REFLECTIVE|TRANSPARENT|THIN_OR_PERFORATED|UNKNOWN",
  "registered_depth_pixel_yx": [0, 0],
  "registered_depth_box_yxyx": [0, 0, 1, 1],
  "same_surface_search_allowed": true
}}

For an unsuitable scene, use material UNKNOWN, confidence 0, null pixel, null
box, same_surface_search_allowed false, and explain why.
""".strip()
