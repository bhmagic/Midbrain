from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from .manager_client import ManagerClient
from .rgb_capture import RgbCapture
from .phase4_policy import Phase4Policy, report_operation_progress
from .vlm_router import VisionLanguageRouter, build_default_vlm_router


class PointingIdentificationSkill:
    """Finite Skill that captures one RGB frame and asks Gemini Robotics-ER."""

    def __init__(
        self,
        capture: RgbCapture,
        model: str,
        *,
        manager: ManagerClient | None = None,
        fallback_camera_provider_id: str = "camera.femto_bolt",
        vlm_router: VisionLanguageRouter | None = None,
    ):
        self.capture = capture
        self.model = model
        self.manager = manager
        self.fallback_camera_provider_id = fallback_camera_provider_id
        self.vlm_router = vlm_router
        self.last_binding: dict[str, Any] | None = None

    async def run(self, user_question: str) -> str:
        skill_id = f"identify-pointed-object-{uuid4()}"
        report_operation_progress("BIND_CAMERA")
        binding = await self._bind_camera(skill_id)
        self.last_binding = dict(binding)
        report_operation_progress("CAPTURE_RGB")
        captured = await self.capture.capture_latest(
            provider_id=self._camera_provider_id(binding),
            binding_id=(
                str(binding.get("binding_id"))
                if binding.get("binding_id") is not None
                else None
            ),
        )
        binding = await self._revalidate_camera_binding(binding)
        self.last_binding = dict(binding)
        validity = binding.get("validity")
        if binding.get("binding_id") is not None and validity not in {
            "CURRENT",
            "FALLBACK_REQUIRES_ACTIVATION",
        }:
            raise RuntimeError(
                "camera capability binding became invalid during capture: "
                f"{validity or 'UNKNOWN'}"
            )
        prompt = self._prompt(user_question)
        report_operation_progress("RUN_VLM")
        inference = await self._router().generate(
            image_bytes=captured.image_bytes,
            mime_type=captured.mime_type,
            prompt=prompt,
        )
        result = {
            "answer": inference.text,
            "screenshot": str(captured.path),
            "frame_id": captured.observation.get("frame_id"),
            "model": inference.model_id,
            "vlm_route": inference.as_dict(),
            "input": "RGB only",
            "skill_id": skill_id,
            "capability_binding": binding,
            "data_route": captured.data_route,
        }
        return json.dumps(result, ensure_ascii=False)

    async def _bind_camera(self, skill_id: str) -> dict[str, Any]:
        if self.manager is None:
            return {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "provider_id": self.fallback_camera_provider_id,
                "reason": "manager client is not configured",
            }
        try:
            binding = await self.manager.bind_capabilities(
                ["camera.rgb"],
                fallback_provider_ids={
                    "camera.rgb": self.fallback_camera_provider_id,
                },
                related_skill_id=skill_id,
            )
            return await self._revalidate_camera_binding(binding)
        except Exception as error:
            return {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "provider_id": self.fallback_camera_provider_id,
                "reason": f"advisory binding unavailable: {error}",
            }

    async def _revalidate_camera_binding(
        self,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        binding_id = binding.get("binding_id")
        if self.manager is None or not isinstance(binding_id, str) or not binding_id:
            return binding
        return await self.manager.capability_binding(binding_id)

    def _camera_provider_id(self, binding: dict[str, Any]) -> str:
        selections = binding.get("selections")
        if isinstance(selections, list):
            for selection in selections:
                if (
                    isinstance(selection, dict)
                    and selection.get("capability") == "camera.rgb"
                    and selection.get("provider_id")
                ):
                    return str(selection["provider_id"])
        return str(binding.get("provider_id") or self.fallback_camera_provider_id)

    def _router(self) -> VisionLanguageRouter:
        if self.vlm_router is not None:
            return self.vlm_router
        policy = Phase4Policy.from_environment()
        return build_default_vlm_router(
            gemini_model=self.model,
            attempt_timeout_s=policy.vlm_attempt_timeout_s,
        )

    @staticmethod
    def _prompt(user_question: str) -> str:
        return f"""
Use only the supplied single RGB image. Do not assume depth, IMU, previous frames,
or hidden sensor data. A person may be pointing at an object in the scene.

User request: {user_question}

Identify the most likely object being pointed at. If the pointing gesture is not
visible or is ambiguous, say so directly and name up to two plausible objects.
Return a concise answer with:
1. object label,
2. brief visual reason,
3. confidence as low, medium, or high.
Do not claim that the robot has moved or interacted with anything.
""".strip()


class VisualSceneAnalysisSkill(PointingIdentificationSkill):
    """General current-frame VLM Skill using the same routed backend boundary."""

    @staticmethod
    def _prompt(user_question: str) -> str:
        return f"""
Use only the supplied single RGB image. Do not assume depth, IMU, previous
frames, world coordinates, or hidden sensor data.

User request: {user_question}

Answer the visual question directly. Separate visible evidence from inference,
state ambiguity, and report confidence as low, medium, or high. Do not claim
that the robot moved or interacted with anything.
""".strip()
