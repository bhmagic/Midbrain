from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from locate_effector_front import (
    parse_effector_front_vlm_result,
    resolve_effector_front_reference,
)

from .phase4_policy import report_operation_progress
from .spatial_registration_adapter import SpatialRegistrationSkillAdapter
from .vlm_router import VisionLanguageRouter


EFFECTOR_FRONT_TEMPORAL_POLICY_ID = "locate-effector-front.input-time.v1"


def build_effector_front_evidence(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
    *,
    valid_region: dict[str, Any] | None,
    maximum_panel_width: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build one lossless VLM image whose coordinates use the depth grid."""

    color = np.asarray(rgb)
    depth = np.asarray(registered_depth_m, dtype=np.float64)
    if color.ndim != 3 or color.shape[2] != 3:
        raise ValueError("rgb must have shape HxWx3")
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    depth_height, depth_width = depth.shape
    if min(depth_height, depth_width) <= 0:
        raise ValueError("registered depth grid must be positive")

    rgb_on_depth = cv2.resize(
        color,
        (depth_width, depth_height),
        interpolation=(
            cv2.INTER_AREA
            if color.shape[0] >= depth_height
            and color.shape[1] >= depth_width
            else cv2.INTER_LINEAR
        ),
    )
    valid = np.isfinite(depth) & (depth >= 0.05) & (depth <= 20.0)
    if valid_region:
        x0 = int(valid_region.get("x") or 0)
        y0 = int(valid_region.get("y") or 0)
        region_width = int(valid_region.get("width") or depth_width)
        region_height = int(valid_region.get("height") or depth_height)
        yy, xx = np.mgrid[0:depth_height, 0:depth_width]
        valid &= (
            (xx >= x0)
            & (xx < x0 + region_width)
            & (yy >= y0)
            & (yy < y0 + region_height)
        )

    depth_gray = np.zeros((depth_height, depth_width), dtype=np.uint8)
    valid_values = depth[valid]
    if valid_values.size:
        near = float(np.percentile(valid_values, 2))
        far = float(np.percentile(valid_values, 98))
        span = max(far - near, 1e-6)
        normalized = np.clip((depth - near) / span, 0.0, 1.0)
        depth_gray[valid] = np.asarray(
            240.0 - normalized[valid] * 185.0,
            dtype=np.uint8,
        )
    depth_panel = np.repeat(depth_gray[:, :, None], 3, axis=2)
    overlay = rgb_on_depth.copy()
    overlay[~valid] = np.asarray(
        overlay[~valid].astype(np.float32) * 0.12,
        dtype=np.uint8,
    )

    if maximum_panel_width is not None and int(maximum_panel_width) <= 0:
        raise ValueError("maximum_panel_width must be positive when provided")
    display_scale = (
        1.0
        if maximum_panel_width is None
        else min(1.0, float(maximum_panel_width) / depth_width)
    )
    panel_width = max(1, int(round(depth_width * display_scale)))
    panel_height = max(1, int(round(depth_height * display_scale)))

    def resize_panel(panel: np.ndarray, interpolation: int) -> np.ndarray:
        return cv2.resize(
            panel,
            (panel_width, panel_height),
            interpolation=interpolation,
        )

    panels = [
        resize_panel(rgb_on_depth, cv2.INTER_AREA),
        resize_panel(depth_panel, cv2.INTER_NEAREST),
        resize_panel(overlay, cv2.INTER_AREA),
    ]
    label_height = 54
    canvas = np.full(
        (panel_height + label_height, panel_width * 3, 3),
        10,
        dtype=np.uint8,
    )
    for index, panel in enumerate(panels):
        canvas[
            label_height : label_height + panel_height,
            index * panel_width : (index + 1) * panel_width,
        ] = panel
    labels = (
        "RGB ON DEPTH GRID",
        "REGISTERED DEPTH",
        "RGB WITH VALID DEPTH",
    )
    for index, label in enumerate(labels):
        cv2.putText(
            canvas,
            label,
            (index * panel_width + 10, 33),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
    if valid_region:
        x0 = int(round(int(valid_region.get("x") or 0) * display_scale))
        y0 = int(round(int(valid_region.get("y") or 0) * display_scale))
        x1 = int(
            round(
                (
                    int(valid_region.get("x") or 0)
                    + int(valid_region.get("width") or depth_width)
                )
                * display_scale
            )
        )
        y1 = int(
            round(
                (
                    int(valid_region.get("y") or 0)
                    + int(valid_region.get("height") or depth_height)
                )
                * display_scale
            )
        )
        for panel_index in (1, 2):
            offset = panel_index * panel_width
            cv2.rectangle(
                canvas,
                (offset + x0, label_height + y0),
                (offset + x1 - 1, label_height + y1 - 1),
                (245, 245, 245),
                2,
            )
    ok, encoded = cv2.imencode(
        ".png",
        cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_PNG_COMPRESSION, 3],
    )
    if not ok:
        raise RuntimeError("could not encode effector-front RGB-D evidence")
    return encoded.tobytes(), {
        "composite_layout": list(labels),
        "rgb_source_grid": [int(color.shape[0]), int(color.shape[1])],
        "registered_depth_grid": [depth_height, depth_width],
        "rgb_resampled_to_registered_depth_grid": (
            color.shape[:2] != depth.shape
        ),
        "panel_display_scale": display_scale,
        "native_depth_grid_pixels_preserved": display_scale == 1.0,
        "coordinate_contract": "ORIGINAL_REGISTERED_DEPTH_PIXEL_YX",
        "valid_region": valid_region,
        "valid_depth_pixels": int(np.count_nonzero(valid)),
        "valid_depth_fraction": float(np.count_nonzero(valid) / valid.size),
        "lossless_encoding": "PNG",
    }


class EffectorFrontSkillAdapter:
    """Locate one general effector-front reference without defining an action point."""

    def __init__(
        self,
        spatial: SpatialRegistrationSkillAdapter,
        router: VisionLanguageRouter,
        *,
        maximum_source_age_at_completion_ms: float = 60_000.0,
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
        self.last_result: dict[str, Any] | None = None

    async def run(self, *, target_frame: str) -> dict[str, Any]:
        if not isinstance(target_frame, str) or not target_frame.strip():
            raise ValueError("target_frame must be non-empty")
        requested_target = target_frame.strip()
        skill_id = f"locate-effector-front-{uuid4()}"
        context = await self.spatial.prepare_context(
            target_frame=requested_target,
            skill_id=skill_id,
        )
        frame = context.frame

        report_operation_progress("BUILD_EFFECTOR_FRONT_RGBD_EVIDENCE")
        image_bytes, evidence = build_effector_front_evidence(
            frame.rgb,
            frame.depth_m,
            valid_region=context.valid_region,
        )
        report_operation_progress("VLM_LOCATE_EFFECTOR_FRONT")
        inference = await self.router.generate(
            image_bytes=image_bytes,
            mime_type="image/png",
            prompt=self._prompt(
                depth_height=int(frame.depth_m.shape[0]),
                depth_width=int(frame.depth_m.shape[1]),
            ),
        )
        vlm_result = parse_effector_front_vlm_result(
            inference.text,
            registered_depth_grid=tuple(
                int(value) for value in frame.depth_m.shape
            ),
        )
        if not vlm_result["scene_suitable"]:
            raise RuntimeError(
                "effector-front VLM rejected the scene: "
                + vlm_result["reason"]
            )

        report_operation_progress("REVALIDATE_EFFECTOR_FRONT_CAMERA_BINDING")
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
                "SOURCE_TOO_OLD_AFTER_VLM: effector-front source age "
                f"{source_age_ms:.1f} ms exceeds "
                f"{self.maximum_source_age_at_completion_ms:.1f} ms"
            )

        report_operation_progress("REGISTER_EFFECTOR_FRONT_DEPTH_POINTS")
        resolved = resolve_effector_front_reference(
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
            target_frame=requested_target,
            calibration_revision=frame.calibration_revision,
            route_provenance=context.selection.as_dict(),
            valid_region=context.valid_region,
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
            "vlm_route": inference.as_dict(),
            "vlm_evidence": evidence,
            "input_temporal_evidence": {
                "policy_id": EFFECTOR_FRONT_TEMPORAL_POLICY_ID,
                "source_observed_at_us": int(frame.timestamp_us),
                "skill_completed_at_us": completed_at_us,
                "source_age_at_completion_ms": source_age_ms,
                "maximum_source_age_at_completion_ms": (
                    self.maximum_source_age_at_completion_ms
                ),
                "spatial_inputs": dict(context.temporal_evidence),
            },
        }
        self.last_result = result
        return result

    @staticmethod
    def _prompt(*, depth_height: int, depth_width: int) -> str:
        return f"""
Locate the general front reference of the visible robot effector assembly.

The image has three panels: RGB resampled onto the registered-depth grid,
registered depth where black means invalid, and RGB dimmed wherever registered
depth is invalid. All returned coordinates MUST be integer [y, x] pixels on the
ORIGINAL registered-depth grid: height={depth_height}, width={depth_width}.

Definition:
- "front" means most distal along the visible rigid assembly away from the
  wrist or arm. It does NOT mean closest to the camera, lowest in the image, or
  the task-specific action point.
- Include a mounted or firmly held tool as part of the effector assembly.
- Select the most distal point that belongs to that rigid assembly and has
  visible valid registered depth.
- If a reflective, shiny, sharp, or thin nominal tip lacks depth, retreat only
  as far as necessary along the same rigid tool. The selected point may be the
  tool body or handle. Never select background, a work object, or a merely
  camera-near surface.
- For a bare two-jaw gripper with two distinct distal jaw fronts, return both.
  Otherwise return one point. Do not return two gripper points when a held or
  mounted tool defines one assembly front.
- If both required bare-gripper fronts cannot be identified with valid depth,
  or the rigid assembly is ambiguous, set scene_suitable false and return no
  points.

Return only one JSON object with exactly this schema:
{{
  "schema": "physical_agent.effector_front_landmark_vlm",
  "schema_version": 1,
  "scene_suitable": true,
  "reason": "brief evidence-based reason",
  "effector_configuration": "BARE_GRIPPER|MOUNTED_TOOL|HELD_TOOL|OTHER_EFFECTOR|UNCERTAIN",
  "front_geometry": "SINGLE_POINT|PAIRED_POINTS|UNKNOWN",
  "depth_fallback_reason": "NONE|SHARP_OR_THIN_FRONT_MISSING_DEPTH|REFLECTIVE_FRONT_MISSING_DEPTH|OTHER_FRONT_MISSING_DEPTH|UNCERTAIN",
  "front_points": [
    {{
      "point_id": "front",
      "registered_depth_pixel_yx": [0, 0],
      "confidence": 0.0,
      "selected_surface": "GRIPPER_TIP|TOOL_TIP|TOOL_BODY_OR_HANDLE|EFFECTOR_BODY|OTHER_RIGID_FRONT",
      "selection_reason": "why this is the most distal valid-depth point"
    }}
  ]
}}

For PAIRED_POINTS use exactly point_id front_1 and front_2, both with
selected_surface GRIPPER_TIP. For an unsuitable scene use configuration
UNCERTAIN, geometry UNKNOWN, fallback UNCERTAIN, and an empty front_points
array.
""".strip()
