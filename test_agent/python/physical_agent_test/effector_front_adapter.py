from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from locate_effector_front import (
    parse_effector_front_vlm_result,
    resolve_effector_front_reference,
)

from .item_locator_adapter import build_item_locator_visual_channels
from .phase4_policy import report_operation_progress
from .spatial_registration_adapter import (
    CAMERA_OPTICAL_CONVENTION_ID,
    WORLD_CONVENTION_ID,
    SpatialRegistrationSkillAdapter,
)
from .vlm_router import VisionLanguageRouter
from .visual_evidence import VisualEvidenceStore


EFFECTOR_FRONT_TEMPORAL_POLICY_ID = "locate-effector-front.input-time.v1"
DEFAULT_MAXIMUM_ARM_RADIUS_M = 1.2
DEFAULT_MAXIMUM_CONTROLLER_VISUAL_SEPARATION_M = 0.15


def apply_controller_consistency_policy(
    resolved: dict[str, Any],
    *,
    controller_reference: dict[str, Any] | None,
    maximum_arm_radius_m: float = DEFAULT_MAXIMUM_ARM_RADIUS_M,
    maximum_controller_visual_separation_m: float = (
        DEFAULT_MAXIMUM_CONTROLLER_VISUAL_SEPARATION_M
    ),
) -> dict[str, Any]:
    """Reject impossible visual points and retain optional FK diagnostics."""

    result = dict(resolved)
    reference = result.get("control_reference")
    target_point = np.asarray(
        (reference or {}).get("target_point_m"),
        dtype=np.float64,
    )
    reasons = list(result.get("quality_reasons") or [])
    checks: dict[str, Any] = {
        "policy": "ARM_RADIUS_REQUIRED_CONTROLLER_FK_ADVISORY_V2",
        "maximum_arm_radius_m": float(maximum_arm_radius_m),
        "maximum_controller_visual_separation_m": float(
            maximum_controller_visual_separation_m
        ),
        "controller_reference": controller_reference,
    }
    rejected = False
    if controller_reference is None:
        reasons.append("CONTROLLER_FK_REFERENCE_UNAVAILABLE")
    if target_point.shape != (3,) or not np.all(np.isfinite(target_point)):
        reasons.append("EFFECTOR_CONTROL_REFERENCE_INVALID")
        rejected = True
    else:
        radius_m = float(np.linalg.norm(target_point))
        checks["visual_reference_radius_from_arm_base_m"] = radius_m
        if radius_m > float(maximum_arm_radius_m):
            reasons.append("EFFECTOR_OUTSIDE_CONFIGURED_ARM_RADIUS")
            rejected = True

        controller_target = np.asarray(
            (controller_reference or {}).get("target_point_m"),
            dtype=np.float64,
        )
        if (
            controller_reference is not None
            and controller_target.shape == (3,)
            and np.all(np.isfinite(controller_target))
        ):
            separation_m = float(
                np.linalg.norm(target_point - controller_target)
            )
            checks["controller_visual_separation_m"] = separation_m
            if separation_m > float(
                maximum_controller_visual_separation_m
            ):
                reasons.append("EFFECTOR_DISAGREES_WITH_CONTROLLER_FK")
                rejected = True

    checks["decision"] = (
        "REJECT"
        if rejected
        else "ACCEPT_DEGRADED_NO_CONTROLLER_FK"
        if controller_reference is None
        else "ACCEPT"
    )
    result["controller_consistency"] = checks
    if rejected:
        result["status"] = "CONTROLLER_CONSISTENCY_REJECTED"
        result["eligible_for_control_math"] = False
        result["motion_usable"] = False
        result["quality_reasons"] = list(dict.fromkeys(reasons))
    elif controller_reference is None:
        try:
            existing_uncertainty = float(
                result.get("uncertainty_radius_m") or 0.0
            )
        except (TypeError, ValueError):
            existing_uncertainty = 0.0
        result["uncertainty_radius_m"] = max(
            0.04,
            existing_uncertainty,
        )
        result["quality_reasons"] = list(dict.fromkeys(reasons))
        result["control_math_policy"] = (
            "BOUNDED_VISUAL_EFFECTOR_WITHOUT_CONTROLLER_FK"
        )
    return result


def build_controller_fk_effector_fallback(
    *,
    controller_reference: dict[str, Any],
    vlm_result: dict[str, Any],
    observed_at_us: int,
    source_frame: str,
    target_frame: str,
    calibration_revision: str | None,
    route_provenance: dict[str, Any],
    maximum_arm_radius_m: float,
    uncertainty_radius_m: float = 0.04,
) -> dict[str, Any]:
    """Use current controller FK when the visible effector has no exact depth."""

    point = np.asarray(
        controller_reference.get("target_point_m"),
        dtype=np.float64,
    )
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise RuntimeError("controller FK fallback has no finite tool point")
    if float(np.linalg.norm(point)) > float(maximum_arm_radius_m):
        raise RuntimeError("controller FK fallback is outside the arm radius")
    uncertainty = float(uncertainty_radius_m)
    if not math.isfinite(uncertainty) or uncertainty <= 0.0:
        raise ValueError("controller FK fallback uncertainty must be positive")
    return {
        "schema": "physical_agent.effector_front_reference",
        "schema_version": 1,
        "status": "CONTROLLER_FK_REFERENCE_READY",
        "eligible_for_control_math": True,
        "motion_usable": False,
        "publishes_control_frame": False,
        "specialized_action_point": False,
        "observed_at_us": int(observed_at_us),
        "source_frame": str(source_frame),
        "target_frame": str(target_frame),
        "calibration_revision": calibration_revision,
        "effector_configuration": str(
            vlm_result.get("effector_configuration") or "UNCERTAIN"
        ),
        "front_geometry": "CONTROLLER_TOOL_FRAME_POINT",
        "depth_fallback_reason": "EXACT_EFFECTOR_DEPTH_UNAVAILABLE",
        "front_points": [],
        "control_reference": {
            "method": "CURRENT_CONTROLLER_FORWARD_KINEMATICS",
            "target_point_m": point.tolist(),
            "pair_separation_m": None,
        },
        "uncertainty_radius_m": uncertainty,
        "quality_reasons": [],
        "quality_warnings": [
            "VISUAL_EFFECTOR_DEPTH_UNAVAILABLE_USING_CONTROLLER_FK"
        ],
        "visual_measurement_usable": False,
        "controller_consistency": {
            "decision": "CONTROLLER_FK_FALLBACK",
            "source": controller_reference.get("source"),
        },
        "vlm_reason": str(vlm_result.get("reason") or ""),
        "data_route": dict(route_provenance),
    }


def build_effector_front_evidence(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
    *,
    valid_region: dict[str, Any] | None,
    maximum_panel_width: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build one depth-validity RGB view in normalized VLM coordinates."""

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
    image_width = max(1, int(round(depth_width * display_scale)))
    image_height = max(1, int(round(depth_height * display_scale)))
    evidence_image = cv2.resize(
        overlay,
        (image_width, image_height),
        interpolation=cv2.INTER_AREA,
    )
    ok, encoded = cv2.imencode(
        ".png",
        cv2.cvtColor(evidence_image, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_PNG_COMPRESSION, 3],
    )
    if not ok:
        raise RuntimeError("could not encode effector-front RGB-D evidence")
    return encoded.tobytes(), {
        "image_layout": "SINGLE_RGB_WITH_INVALID_DEPTH_DIMMED",
        "image_grid": [int(image_height), int(image_width)],
        "rgb_source_grid": [int(color.shape[0]), int(color.shape[1])],
        "registered_depth_grid": [depth_height, depth_width],
        "rgb_resampled_to_registered_depth_grid": (
            color.shape[:2] != depth.shape
        ),
        "panel_display_scale": display_scale,
        "native_depth_grid_pixels_preserved": display_scale == 1.0,
        "coordinate_contract": "NORMALIZED_0_1000_YX",
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
        evidence_dir: Path | None = None,
        arm_tool_frame: str = "rebot_arm_tool",
        maximum_arm_radius_m: float = DEFAULT_MAXIMUM_ARM_RADIUS_M,
        maximum_controller_visual_separation_m: float = (
            DEFAULT_MAXIMUM_CONTROLLER_VISUAL_SEPARATION_M
        ),
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
        self.arm_tool_frame = str(arm_tool_frame).strip()
        self.maximum_arm_radius_m = float(maximum_arm_radius_m)
        self.maximum_controller_visual_separation_m = float(
            maximum_controller_visual_separation_m
        )
        self.visual_evidence_store = visual_evidence_store
        if not self.arm_tool_frame:
            raise ValueError("arm_tool_frame must be non-empty")
        if self.maximum_arm_radius_m <= 0.0:
            raise ValueError("maximum_arm_radius_m must be positive")
        if self.maximum_controller_visual_separation_m <= 0.0:
            raise ValueError(
                "maximum_controller_visual_separation_m must be positive"
            )
        self.last_result: dict[str, Any] | None = None

    async def run(
        self,
        *,
        target_frame: str,
        spatial_context: Any | None = None,
    ) -> dict[str, Any]:
        if not isinstance(target_frame, str) or not target_frame.strip():
            raise ValueError("target_frame must be non-empty")
        requested_target = target_frame.strip()
        skill_id = f"locate-effector-front-{uuid4()}"
        context = spatial_context
        if context is None:
            context = await self.spatial.prepare_context(
                target_frame=requested_target,
                skill_id=skill_id,
            )
        elif str(getattr(context, "target_frame", "")) != requested_target:
            raise ValueError("shared spatial context target frame changed")
        frame = context.frame
        controller_reference = await self._controller_reference(context)

        report_operation_progress("BUILD_EFFECTOR_FRONT_RGBD_EVIDENCE")
        image_bytes, evidence = build_effector_front_evidence(
            frame.rgb,
            frame.depth_m,
            valid_region=context.valid_region,
        )
        evidence_image: dict[str, Any] | None = None
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_path = (
                self.evidence_dir / f"{skill_id}-evidence.png"
            )
            evidence_path.write_bytes(image_bytes)
            evidence_image = {
                "path": str(evidence_path),
                "mime_type": "image/png",
                "purpose": (
                    "RGB_DEPTH_VALIDITY_EFFECTOR_LOCALIZATION_EVIDENCE"
                ),
            }
        report_operation_progress("VLM_LOCATE_EFFECTOR_FRONT")
        inference = await self.router.generate(
            image_bytes=image_bytes,
            mime_type="image/png",
            prompt=self._prompt(
                depth_height=int(frame.depth_m.shape[0]),
                depth_width=int(frame.depth_m.shape[1]),
                controller_reference=controller_reference,
            ),
        )
        vlm_result = parse_effector_front_vlm_result(
            inference.text,
            registered_depth_grid=tuple(
                int(value) for value in frame.depth_m.shape
            ),
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
        try:
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
                target_frame=context.target_frame,
                calibration_revision=frame.calibration_revision,
                route_provenance=context.selection.as_dict(),
                valid_region=context.valid_region,
            )
        except RuntimeError as error:
            depth_limited = (
                not vlm_result.get("scene_suitable")
                or "no valid exact depth" in str(error)
            )
            if controller_reference is None or not depth_limited:
                raise
            resolved = build_controller_fk_effector_fallback(
                controller_reference=controller_reference,
                vlm_result=vlm_result,
                observed_at_us=int(frame.timestamp_us),
                source_frame=str(frame.camera_frame),
                target_frame=context.target_frame,
                calibration_revision=frame.calibration_revision,
                route_provenance=context.selection.as_dict(),
                maximum_arm_radius_m=self.maximum_arm_radius_m,
            )
        if (
            context.target_frame == "rebot_arm_base"
            and resolved.get("status") != "CONTROLLER_FK_REFERENCE_READY"
        ):
            resolved = apply_controller_consistency_policy(
                resolved,
                controller_reference=controller_reference,
                maximum_arm_radius_m=self.maximum_arm_radius_m,
                maximum_controller_visual_separation_m=(
                    self.maximum_controller_visual_separation_m
                ),
            )
            if (
                resolved.get("status") == "CONTROLLER_CONSISTENCY_REJECTED"
                and controller_reference is not None
            ):
                rejected_visual_reference = resolved
                resolved = build_controller_fk_effector_fallback(
                    controller_reference=controller_reference,
                    vlm_result=vlm_result,
                    observed_at_us=int(frame.timestamp_us),
                    source_frame=str(frame.camera_frame),
                    target_frame=context.target_frame,
                    calibration_revision=frame.calibration_revision,
                    route_provenance=context.selection.as_dict(),
                    maximum_arm_radius_m=self.maximum_arm_radius_m,
                )
                resolved["depth_fallback_reason"] = (
                    "VISUAL_EFFECTOR_INCONSISTENT_WITH_CONTROLLER_FK"
                )
                resolved["quality_warnings"] = [
                    "VISUAL_EFFECTOR_INCONSISTENT_USING_CONTROLLER_FK"
                ]
                resolved["rejected_visual_reference"] = (
                    rejected_visual_reference
                )
        visual_evidence = None
        if self.visual_evidence_store is not None:
            visual_evidence = await self.visual_evidence_store.register_channels(
                channels=build_item_locator_visual_channels(
                    frame.rgb,
                    frame.depth_m,
                ),
                default_channel="rgb_depth",
                title="Effector-front localization",
                annotations=_effector_annotations(
                    vlm_result,
                    depth_grid=frame.depth_m.shape,
                ),
                confidence=_effector_visual_confidence(vlm_result),
                model=inference.model_id,
                source_skill="locate_effector_front",
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
                "source_front_points": vlm_result.get(
                    "source_front_points"
                ),
                "registered_depth_front_points": vlm_result.get(
                    "front_points"
                ),
                "conversion": vlm_result.get("coordinate_conversion"),
            },
            "vlm_evidence": evidence,
            "evidence_image": evidence_image,
            "visual_evidence": visual_evidence,
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

    async def _controller_reference(self, context: Any) -> dict[str, Any] | None:
        if context.target_frame != "rebot_arm_base":
            return None
        frame = context.frame
        capture_time_error: str | None = None

        async def resolve(
            at_us: int | None,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            return await asyncio.gather(
                self.spatial.fabric.transform(
                    from_frame=self.arm_tool_frame,
                    to_frame=context.target_frame,
                    at_us=at_us,
                    max_extrapolation_us=(
                        self.spatial.maximum_transform_extrapolation_us
                    ),
                    session_epoch=None,
                ),
                self.spatial.fabric.transform(
                    from_frame=self.arm_tool_frame,
                    to_frame=str(frame.camera_frame),
                    at_us=at_us,
                    max_extrapolation_us=(
                        self.spatial.maximum_transform_extrapolation_us
                    ),
                    session_epoch=None,
                ),
            )

        try:
            target_transform, camera_transform = await resolve(
                int(frame.timestamp_us)
            )
            source = "CAPTURE_TIME_FABRIC_CONTROLLER_FORWARD_KINEMATICS"
        except Exception as error:
            capture_time_error = str(error)
            try:
                target_transform, camera_transform = await resolve(None)
                source = "LATEST_FABRIC_CONTROLLER_FORWARD_KINEMATICS"
            except Exception:
                return None
        target_point = np.asarray(
            target_transform.get("translation_m"),
            dtype=np.float64,
        )
        camera_point = np.asarray(
            camera_transform.get("translation_m"),
            dtype=np.float64,
        )
        if (
            target_point.shape != (3,)
            or camera_point.shape != (3,)
            or not np.all(np.isfinite(target_point))
            or not np.all(np.isfinite(camera_point))
        ):
            return None
        projected_pixel_yx: list[float] | None = None
        if camera_point[2] > 0.0:
            intrinsics = frame.intrinsics
            projected_pixel_yx = [
                float(
                    intrinsics["fy"] * camera_point[1] / camera_point[2]
                    + intrinsics["cy"]
                ),
                float(
                    intrinsics["fx"] * camera_point[0] / camera_point[2]
                    + intrinsics["cx"]
                ),
            ]
        return {
            "source": source,
            "arm_tool_frame": self.arm_tool_frame,
            "target_frame": context.target_frame,
            "target_point_m": target_point.tolist(),
            "camera_frame": str(frame.camera_frame),
            "camera_point_m": camera_point.tolist(),
            "projected_registered_depth_pixel_yx": projected_pixel_yx,
            "observed_at_us": int(frame.timestamp_us),
            "capture_time_query_error": capture_time_error,
            "temporal_policy": (
                "CAPTURE_TIME_EXACT"
                if capture_time_error is None
                else "FABRIC_BEST_AVAILABLE_LATEST_FALLBACK"
            ),
        }

    @staticmethod
    def _prompt(
        *,
        depth_height: int,
        depth_width: int,
        controller_reference: dict[str, Any] | None = None,
    ) -> str:
        controller_guidance = ""
        if controller_reference is not None:
            projected = controller_reference.get(
                "projected_registered_depth_pixel_yx"
            )
            normalized_prior = None
            if isinstance(projected, list) and len(projected) == 2:
                projected_y, projected_x = (
                    float(projected[0]),
                    float(projected[1]),
                )
                if (
                    math.isfinite(projected_y)
                    and math.isfinite(projected_x)
                    and 0.0 <= projected_y < depth_height
                    and 0.0 <= projected_x < depth_width
                ):
                    normalized_prior = [
                        int(
                            round(
                                projected_y
                                * 1000.0
                                / max(1, depth_height - 1)
                            )
                        ),
                        int(
                            round(
                                projected_x
                                * 1000.0
                                / max(1, depth_width - 1)
                            )
                        ),
                    ]
            controller_guidance = f"""
Controller forward kinematics predicts the robot tool frame near normalized
image coordinate {normalized_prior}
at camera depth {controller_reference['camera_point_m'][2]:.3f} m. Use this as
a bounded association prior: the visible effector front must be part of the
same connected rigid assembly and should remain within roughly 0.4 m in 3D of
that predicted tool frame. Reject the scene instead of selecting background or
another gripper-like object that violates this prior.
""".strip()
        return f"""
Locate the general front reference of the visible robot effector assembly.

The image is one RGB view resampled onto the registered-depth grid. Pixels
without valid registered depth are strongly dimmed. Return every point as an
integer [y, x] coordinate in NORMALIZED_0_1000 image space, independent of the
displayed or encoded resolution. The deterministic host maps this normalized
space onto the registered-depth grid of height={depth_height},
width={depth_width}.

{controller_guidance}

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
  "schema_version": 2,
  "coordinate_space": "NORMALIZED_0_1000",
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


def _effector_visual_confidence(vlm_result: dict[str, Any]) -> str:
    points = vlm_result.get("front_points")
    points = points if isinstance(points, list) else []
    values: list[float] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        try:
            values.append(float(point.get("confidence")))
        except (TypeError, ValueError):
            continue
    if not values:
        return "low"
    confidence = min(values)
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.60:
        return "medium"
    return "low"


def _effector_annotations(
    vlm_result: dict[str, Any],
    *,
    depth_grid: tuple[int, ...],
) -> list[dict[str, Any]]:
    height, width = (int(depth_grid[0]), int(depth_grid[1]))
    annotations: list[dict[str, Any]] = []
    points = vlm_result.get("front_points")
    points = points if isinstance(points, list) else []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            continue
        pixel = point.get("registered_depth_pixel_yx")
        if not isinstance(pixel, list) or len(pixel) != 2:
            continue
        y, x = (int(pixel[0]), int(pixel[1]))
        annotations.append(
            {
                "id": str(point.get("point_id") or f"front-{index + 1}"),
                "type": "point",
                "label": str(
                    point.get("selected_surface") or "effector front"
                ),
                "confidence": _effector_visual_confidence(
                    {"front_points": [point]}
                ),
                "applies_to_channels": ["rgb", "depth", "rgb_depth"],
                "x": (x + 0.5) / width,
                "y": (y + 0.5) / height,
            }
        )
    return annotations
