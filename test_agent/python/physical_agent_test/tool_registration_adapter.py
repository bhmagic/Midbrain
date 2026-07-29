from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import cv2
import numpy as np
from register_tool_to_control_frame import (
    register_tool_to_control_frame_candidate,
)

from .phase4_policy import report_operation_progress
from .spatial_registration_adapter import (
    BindingSnapshot,
    SpatialFrameContext,
    SpatialRegistrationSkillAdapter,
)
from .vlm_router import VisionLanguageRouter


ARM_TRANSFORM_CAPABILITY = "robot_arm.transforms.local"


class ToolRegistrationManager(Protocol):
    async def bind_capabilities(
        self,
        required_capabilities: list[str],
        *,
        fallback_provider_ids: dict[str, str] | None = None,
        allowed_provider_ids: list[str] | None = None,
        excluded_provider_ids: list[str] | None = None,
        request_id: str | None = None,
        related_skill_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def capability_binding(self, binding_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ToolTransformEvidence:
    target_from_tool: np.ndarray
    base_from_tool_query: dict[str, Any]
    target_from_base_query: dict[str, Any]


def parse_tool_landmark_result(text: str) -> dict[str, Any]:
    candidate = str(text).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match is None:
            raise RuntimeError("tool-landmark VLM did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("tool-landmark VLM result must be a JSON object")
    landmarks = value.get("landmarks")
    if not isinstance(landmarks, list):
        raise RuntimeError("tool-landmark VLM result has no landmarks array")
    expected_roles = {"acting_point", "axis_reference", "plane_reference"}
    observed_roles: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for landmark in landmarks:
        if not isinstance(landmark, dict):
            raise RuntimeError("tool landmark must be an object")
        role = str(landmark.get("role") or "")
        if role not in expected_roles or role in observed_roles:
            raise RuntimeError("tool landmark roles are missing, unknown, or duplicated")
        pixel = landmark.get("pixel_yx")
        if (
            not isinstance(pixel, list)
            or len(pixel) != 2
            or not all(isinstance(item, (int, float)) for item in pixel)
        ):
            raise RuntimeError(f"tool landmark {role} has invalid pixel_yx")
        confidence = float(landmark.get("confidence") or 0.0)
        if not 0.0 <= confidence <= 1.0:
            raise RuntimeError(f"tool landmark {role} confidence is out of range")
        normalized.append(
            {
                "role": role,
                "pixel_yx": [float(pixel[0]), float(pixel[1])],
                "confidence": confidence,
                "depth_policy": str(
                    landmark.get("depth_policy") or "ROBUST_MEDIAN"
                ).upper(),
            }
        )
        observed_roles.add(role)
    if observed_roles != expected_roles:
        raise RuntimeError("tool-landmark VLM did not identify all required roles")
    valid_policies = {
        "ROBUST_MEDIAN",
        "CLOSEST_TO_CAMERA",
        "NEAREST_VALID_PIXEL",
    }
    if any(item["depth_policy"] not in valid_policies for item in normalized):
        raise RuntimeError("tool-landmark VLM selected an unsupported depth policy")
    return {
        "landmarks": normalized,
        "scene_suitable": bool(value.get("scene_suitable")),
        "reason": str(value.get("reason") or "").strip(),
    }


class ToolControlFrameSkillAdapter:
    """Create a VLM/RGB-D review candidate without publishing a control frame."""

    def __init__(
        self,
        spatial: SpatialRegistrationSkillAdapter,
        router: VisionLanguageRouter,
        *,
        manager: ToolRegistrationManager | None,
        fallback_arm_provider_id: str,
        arm_base_frame: str,
        arm_tool_frame: str,
        binding_mode: str = "SHADOW",
        maximum_transform_extrapolation_us: int = 750_000,
    ):
        normalized_mode = str(binding_mode).strip().upper()
        if normalized_mode not in {"SHADOW", "ENFORCED", "FALLBACK"}:
            raise ValueError(
                "binding_mode must be SHADOW, ENFORCED, or FALLBACK"
            )
        self.spatial = spatial
        self.router = router
        self.manager = manager
        self.fallback_arm_provider_id = str(fallback_arm_provider_id)
        self.arm_base_frame = str(arm_base_frame)
        self.arm_tool_frame = str(arm_tool_frame)
        self.binding_mode = normalized_mode
        self.maximum_transform_extrapolation_us = int(
            maximum_transform_extrapolation_us
        )
        self.last_result: dict[str, Any] | None = None
        self.last_binding: dict[str, Any] | None = None

    async def run(
        self,
        *,
        tool_description: str,
        control_frame_purpose: str,
        target_frame: str,
    ) -> dict[str, Any]:
        description = str(tool_description).strip()
        purpose = str(control_frame_purpose).strip()
        requested_target = str(target_frame).strip()
        if not description or not purpose or not requested_target:
            raise ValueError(
                "tool_description, control_frame_purpose, and target_frame "
                "must be non-empty"
            )
        skill_id = f"register-tool-control-frame-{uuid4()}"

        report_operation_progress("BIND_ROBOT_TRANSFORM_PROVIDER")
        arm_binding = await self._bind_arm(skill_id)
        self.last_binding = dict(arm_binding.binding)

        context = await self.spatial.prepare_context(
            target_frame=requested_target,
            skill_id=skill_id,
        )
        frame = context.frame
        report_operation_progress("ENCODE_TOOL_REGISTRATION_RGB")
        image_bytes = self._encode_rgb(frame.rgb)
        report_operation_progress("VLM_IDENTIFY_TOOL_LANDMARKS")
        inference = await self.router.generate(
            image_bytes=image_bytes,
            mime_type="image/jpeg",
            prompt=self._prompt(
                description,
                purpose,
                rgb_height=int(frame.rgb.shape[0]),
                rgb_width=int(frame.rgb.shape[1]),
            ),
        )
        landmarks = parse_tool_landmark_result(inference.text)
        if not landmarks["scene_suitable"]:
            raise RuntimeError(
                "tool-landmark VLM rejected the scene: "
                + (landmarks["reason"] or "no reason supplied")
            )

        report_operation_progress("REVALIDATE_TOOL_CAMERA_BINDING")
        camera_binding = await self.spatial.revalidate_context_binding(
            context
        )
        report_operation_progress("REVALIDATE_ROBOT_TRANSFORM_BINDING")
        arm_binding = await self._revalidate_arm_binding(arm_binding)
        self.last_binding = dict(arm_binding.binding)
        transform_evidence = await self._tool_transform(
            context,
            arm_binding=arm_binding,
        )

        report_operation_progress("BUILD_REVIEW_ONLY_TOOL_FRAME_CANDIDATE")
        vlm_result = {
            **landmarks,
            "backend_id": inference.backend_id,
            "model": inference.model_id,
            "request_id": skill_id,
        }
        candidate = register_tool_to_control_frame_candidate(
            vlm_result=vlm_result,
            rgb_grid=tuple(int(value) for value in frame.rgb.shape[:2]),
            registered_depth_m=frame.depth_m,
            registered_depth_grid=tuple(
                int(value) for value in frame.depth_m.shape[:2]
            ),
            intrinsics=dict(frame.intrinsics),
            target_from_camera=context.target_from_camera,
            target_from_tool=transform_evidence.target_from_tool,
            observed_at_us=int(frame.timestamp_us),
            source_frame=str(frame.camera_frame),
            target_frame=requested_target,
            calibration_revision=frame.calibration_revision,
            route_provenance=context.selection.as_dict(),
            geometry={
                "axis_start_role": "acting_point",
                "axis_end_role": "axis_reference",
                "plane_role": "plane_reference",
                "origin_from_axis_start_m": 0.0,
                "plane_axis_sign": 1.0,
                "minimum_landmark_confidence": 0.75,
                "minimum_axis_length_m": 0.01,
                "minimum_plane_offset_m": 0.005,
                "maximum_tool_to_origin_m": 0.75,
            },
            valid_region=context.valid_region,
        )
        result = {
            **candidate,
            "skill_id": skill_id,
            "tool_description": description,
            "control_frame_purpose": purpose,
            "safety_class": "READ_ONLY",
            "physical_action_submitted": False,
            "control_frame_published": False,
            "camera_capability_binding": camera_binding.as_dict(),
            "arm_capability_binding": arm_binding.as_dict(),
            "binding_mode": self.binding_mode,
            "camera_capture": self.spatial.capture_provenance(context),
            "camera_transform_provenance": (
                self.spatial.transform_provenance(context)
            ),
            "tool_transform_provenance": {
                "base_from_tool": transform_evidence.base_from_tool_query,
                "target_from_base": transform_evidence.target_from_base_query,
                "composition": (
                    "target_from_base @ base_from_tool; robot boot and VIO "
                    "epochs remain separate"
                ),
            },
            "selected_route_metadata": self.spatial.route_metadata(context),
            "vlm_route": inference.as_dict(),
        }
        self.last_result = result
        return result

    async def _bind_arm(self, skill_id: str) -> BindingSnapshot:
        if self.manager is None:
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    "tool binding enforcement requires an available Manager"
                )
            binding = {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.fallback_arm_provider_id,
                "reason": "Manager client is not configured",
            }
            return BindingSnapshot(
                provider_id=self.fallback_arm_provider_id,
                provider_instance_id=None,
                boot_id=None,
                binding=binding,
                enforcement_issues=("MANAGER_BINDING_UNAVAILABLE",),
                configured_fallback_provider_ids={
                    ARM_TRANSFORM_CAPABILITY: self.fallback_arm_provider_id,
                },
            )
        try:
            binding = await self.manager.bind_capabilities(
                [ARM_TRANSFORM_CAPABILITY],
                fallback_provider_ids={
                    ARM_TRANSFORM_CAPABILITY: self.fallback_arm_provider_id,
                },
                related_skill_id=skill_id,
            )
            binding_id = binding.get("binding_id")
            if isinstance(binding_id, str) and binding_id:
                binding = await self.manager.capability_binding(binding_id)
            return self._arm_binding_snapshot(binding)
        except Exception as error:
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    f"tool binding enforcement rejected fallback: {error}"
                ) from error
            binding = {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.fallback_arm_provider_id,
                "reason": f"Manager binding unavailable: {error}",
            }
            return BindingSnapshot(
                provider_id=self.fallback_arm_provider_id,
                provider_instance_id=None,
                boot_id=None,
                binding=binding,
                enforcement_issues=("MANAGER_BINDING_UNAVAILABLE",),
                configured_fallback_provider_ids={
                    ARM_TRANSFORM_CAPABILITY: self.fallback_arm_provider_id,
                },
            )

    async def _revalidate_arm_binding(
        self,
        current: BindingSnapshot,
    ) -> BindingSnapshot:
        binding_id = current.binding.get("binding_id")
        if (
            self.manager is None
            or not isinstance(binding_id, str)
            or not binding_id
        ):
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    "tool binding enforcement requires a revalidatable binding"
                )
            return current
        return self._arm_binding_snapshot(
            await self.manager.capability_binding(binding_id)
        )

    def _arm_binding_snapshot(
        self,
        binding: dict[str, Any],
    ) -> BindingSnapshot:
        selections = binding.get("selections")
        selection = next(
            (
                item
                for item in selections
                if isinstance(item, dict)
                and item.get("capability") == ARM_TRANSFORM_CAPABILITY
            ),
            None,
        ) if isinstance(selections, list) else None
        issues: list[str] = []
        if not isinstance(selection, dict):
            issues.append("ARM_TRANSFORM_CAPABILITY_UNRESOLVED")
            provider_id = str(
                binding.get("provider_id") or self.fallback_arm_provider_id
            )
            instance_id = None
            boot_id = None
        else:
            provider_id = str(selection.get("provider_id") or "")
            instance_id = (
                str(selection.get("provider_instance_id"))
                if selection.get("provider_instance_id")
                else None
            )
            boot_id = (
                str(selection.get("boot_id"))
                if selection.get("boot_id")
                else None
            )
            if not provider_id or not instance_id or not boot_id:
                issues.append("ARM_PROVIDER_IDENTITY_INCOMPLETE")
        validity = str(binding.get("validity") or "")
        if validity != "CURRENT":
            issues.append(f"BINDING_NOT_CURRENT:{validity or 'UNKNOWN'}")
        if str(binding.get("status") or "") != "RESOLVED":
            issues.append(
                f"BINDING_NOT_RESOLVED:{binding.get('status') or 'UNKNOWN'}"
            )
        snapshot = BindingSnapshot(
            provider_id=provider_id,
            provider_instance_id=instance_id,
            boot_id=boot_id,
            binding=dict(binding),
            enforcement_issues=tuple(issues),
            configured_fallback_provider_ids={
                ARM_TRANSFORM_CAPABILITY: self.fallback_arm_provider_id,
            },
        )
        if self.binding_mode == "ENFORCED" and issues:
            raise RuntimeError(
                "tool binding enforcement failed: " + "; ".join(issues)
            )
        return snapshot

    async def _tool_transform(
        self,
        context: SpatialFrameContext,
        *,
        arm_binding: BindingSnapshot,
    ) -> ToolTransformEvidence:
        frame = context.frame
        base_from_tool = await self.spatial.fabric.transform(
            from_frame=self.arm_tool_frame,
            to_frame=self.arm_base_frame,
            at_us=int(frame.timestamp_us),
            max_extrapolation_us=self.maximum_transform_extrapolation_us,
        )
        self.spatial._validate_transform(
            base_from_tool,
            source_frame=self.arm_tool_frame,
            target_frame=self.arm_base_frame,
            timestamp_us=int(frame.timestamp_us),
            session_epoch=None,
        )
        self._validate_arm_transform_identity(base_from_tool, arm_binding)

        target_from_base = await self.spatial.fabric.transform(
            from_frame=self.arm_base_frame,
            to_frame=context.target_frame,
            at_us=int(frame.timestamp_us),
            max_extrapolation_us=self.maximum_transform_extrapolation_us,
            session_epoch=str(frame.session_epoch),
        )
        self.spatial._validate_transform(
            target_from_base,
            source_frame=self.arm_base_frame,
            target_frame=context.target_frame,
            timestamp_us=int(frame.timestamp_us),
            session_epoch=str(frame.session_epoch),
        )
        return ToolTransformEvidence(
            target_from_tool=(
                self.spatial._transform_matrix(target_from_base)
                @ self.spatial._transform_matrix(base_from_tool)
            ),
            base_from_tool_query=base_from_tool,
            target_from_base_query=target_from_base,
        )

    def _validate_arm_transform_identity(
        self,
        transform: dict[str, Any],
        binding: BindingSnapshot,
    ) -> None:
        matching_steps = [
            step
            for step in transform.get("path") or []
            if isinstance(step, dict)
            and (
                step.get("from_frame") == self.arm_tool_frame
                or step.get("to_frame") == self.arm_tool_frame
                or step.get("child_frame") == self.arm_tool_frame
            )
        ]
        if not matching_steps:
            raise RuntimeError("robot tool transform has no tool-edge provenance")
        for step in matching_steps:
            provider_id = str(step.get("provider_id") or "")
            instance_id = str(step.get("provider_instance_id") or "")
            if provider_id != binding.provider_id:
                raise RuntimeError(
                    "robot tool transform provider does not match binding"
                )
            if (
                binding.provider_instance_id
                and instance_id != binding.provider_instance_id
            ):
                raise RuntimeError(
                    "robot tool transform instance does not match binding"
                )

    @staticmethod
    def _encode_rgb(rgb: np.ndarray) -> bytes:
        ok, encoded = cv2.imencode(
            ".jpg",
            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, 92],
        )
        if not ok:
            raise RuntimeError("could not encode tool-registration RGB frame")
        return encoded.tobytes()

    @staticmethod
    def _prompt(
        tool_description: str,
        control_frame_purpose: str,
        *,
        rgb_height: int,
        rgb_width: int,
    ) -> str:
        return f"""
Use only this RGB image. Identify three visible landmarks on the mounted tool.

Tool description: {tool_description}
Desired control-frame purpose: {control_frame_purpose}
Image grid: height={rgb_height}, width={rgb_width}. Return actual pixel
coordinates as [y, x], not normalized coordinates.

Required roles:
- acting_point: the physical point where the described action should originate;
- axis_reference: a distinct point along the intended forward/tool axis;
- plane_reference: a distinct non-collinear point defining the tool plane.

Reflective tool surfaces may have invalid depth. Select CLOSEST_TO_CAMERA when a
nearby valid surface is more trustworthy than the exact shiny pixel; otherwise
use ROBUST_MEDIAN or NEAREST_VALID_PIXEL. Do not infer hidden geometry. If all
three points are not clearly visible and non-collinear, set scene_suitable false.

Return only one JSON object:
{{
  "scene_suitable": true,
  "reason": "brief visual justification",
  "landmarks": [
    {{"role": "acting_point", "pixel_yx": [0, 0], "confidence": 0.0,
      "depth_policy": "CLOSEST_TO_CAMERA"}},
    {{"role": "axis_reference", "pixel_yx": [0, 0], "confidence": 0.0,
      "depth_policy": "ROBUST_MEDIAN"}},
    {{"role": "plane_reference", "pixel_yx": [0, 0], "confidence": 0.0,
      "depth_policy": "ROBUST_MEDIAN"}}
  ]
}}
""".strip()
