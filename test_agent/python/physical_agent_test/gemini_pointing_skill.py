from __future__ import annotations

import asyncio
import io
import json
import math
import os
from typing import Any
from uuid import uuid4

from PIL import Image

from .manager_client import ManagerClient
from .phase4_policy import Phase4Policy, report_operation_progress
from .rgb_capture import CameraObservationUnavailable, RgbCapture
from .visual_evidence import VisualEvidenceStore
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
        visual_evidence_store: VisualEvidenceStore | None = None,
        capture_attempts: int = 2,
        capture_retry_backoff_s: float = 0.25,
    ):
        if not 1 <= capture_attempts <= 3:
            raise ValueError("capture_attempts must be between 1 and 3")
        if not 0.0 <= capture_retry_backoff_s <= 5.0:
            raise ValueError(
                "capture_retry_backoff_s must be between 0 and 5 seconds"
            )
        self.capture = capture
        self.model = model
        self.manager = manager
        self.fallback_camera_provider_id = fallback_camera_provider_id
        self.vlm_router = vlm_router
        self.visual_evidence_store = visual_evidence_store
        self.capture_attempts = capture_attempts
        self.capture_retry_backoff_s = capture_retry_backoff_s
        self.last_binding: dict[str, Any] | None = None

    async def run(self, user_question: str) -> str:
        skill_id = f"identify-pointed-object-{uuid4()}"
        report_operation_progress("BIND_CAMERA")
        binding = await self._bind_camera(skill_id)
        self.last_binding = dict(binding)
        if binding.get("validity") == "FALLBACK_REQUIRES_ACTIVATION":
            return self._activation_required_result(binding, skill_id)
        captured, capture_retry, capture_error = await self._capture_rgb(
            binding
        )
        if captured is None:
            assert capture_error is not None
            return self._frame_unavailable_result(
                binding,
                skill_id,
                capture_error,
                capture_retry,
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
        width, height = self._image_dimensions(captured)
        visual_result = self._parse_visual_result(
            inference.text,
            image_width=width,
            image_height=height,
            coordinate_hint=self._model_coordinate_hint(inference.model_id),
        )
        result = {
            "answer": visual_result["answer"],
            "confidence": visual_result["confidence"],
            "annotations": visual_result["annotations"],
            "annotation_processing": visual_result["annotation_processing"],
            "screenshot": str(captured.path),
            "frame_id": captured.observation.get("frame_id"),
            "model": inference.model_id,
            "vlm_route": inference.as_dict(),
            "input": "RGB only",
            "skill_id": skill_id,
            "capability_binding": binding,
            "data_route": captured.data_route,
        }
        if capture_retry is not None:
            result["retry_history"] = capture_retry
        if self.visual_evidence_store is not None:
            result["visual_evidence"] = (
                await self.visual_evidence_store.register_rgb(
                    image_bytes=captured.image_bytes,
                    media_type=captured.mime_type,
                    width=width,
                    height=height,
                    title=self._evidence_title(),
                    annotations=visual_result["annotations"],
                    confidence=visual_result["confidence"],
                    model=inference.model_id,
                    source_skill=self._source_skill_id(),
                )
            )
        return json.dumps(result, ensure_ascii=False)

    async def _capture_rgb(
        self,
        binding: dict[str, Any],
    ) -> tuple[
        Any | None,
        dict[str, Any] | None,
        CameraObservationUnavailable | None,
    ]:
        attempts: list[dict[str, Any]] = []
        last_error: CameraObservationUnavailable | None = None
        for attempt in range(1, self.capture_attempts + 1):
            report_operation_progress(
                "CAPTURE_RGB" if attempt == 1 else "CAPTURE_RGB_RETRY"
            )
            try:
                captured = await self.capture.capture_latest(
                    provider_id=self._camera_provider_id(binding),
                    binding_id=(
                        str(binding.get("binding_id"))
                        if binding.get("binding_id") is not None
                        else None
                    ),
                )
            except CameraObservationUnavailable as error:
                last_error = error
                attempts.append(
                    {
                        "attempt": attempt,
                        "outcome": "camera_frame_unavailable",
                    }
                )
                if attempt < self.capture_attempts:
                    await asyncio.sleep(self.capture_retry_backoff_s)
                continue

            if not attempts:
                return captured, None, None
            attempts.append({"attempt": attempt, "outcome": "succeeded"})
            return captured, self._capture_retry_history(
                attempts,
                recovered=True,
            ), None

        return None, self._capture_retry_history(
            attempts,
            recovered=False,
        ), last_error

    def _capture_retry_history(
        self,
        attempts: list[dict[str, Any]],
        *,
        recovered: bool,
    ) -> dict[str, Any]:
        return {
            "scope": "CAPTURE_RGB_ONLY",
            "attempt_count": len(attempts),
            "maximum_attempts": self.capture_attempts,
            "recovered": recovered,
            "exhausted": not recovered,
            "requires_fresh_evidence": True,
            "physical_action_submitted": False,
            "attempts": attempts,
        }

    def _activation_required_result(
        self,
        binding: dict[str, Any],
        skill_id: str,
    ) -> str:
        provider_id = self._camera_provider_id(binding)
        manager_url = str(
            getattr(self.manager, "base_url", "http://127.0.0.1:7001")
        ).rstrip("/")
        return json.dumps(
            {
                "status": "PROVIDER_ACTIVATION_REQUIRED",
                "skill_id": skill_id,
                "required_capability": "camera.rgb",
                "provider_id": provider_id,
                "message": (
                    "Visual analysis needs a live RGB observation. The camera "
                    "Provider is currently cold or has not published a frame."
                ),
                "developer_activation_url": (
                    f"{manager_url}/developer/provider/{provider_id}"
                ),
                "physical_action_submitted": False,
                "capability_binding": binding,
            },
            ensure_ascii=False,
        )

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
            attempts_per_backend=policy.vlm_attempts_per_backend,
            retry_backoff_s=policy.vlm_retry_backoff_s,
        )

    def _frame_unavailable_result(
        self,
        binding: dict[str, Any],
        skill_id: str,
        error: CameraObservationUnavailable,
        retry_history: dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "status": "CAMERA_FRAME_UNAVAILABLE",
                "skill_id": skill_id,
                "required_capability": "camera.rgb",
                "provider_id": self._camera_provider_id(binding),
                "message": (
                    "The camera Provider was selected, but no readable RGB "
                    "frame arrived before the bounded warm-up timeout."
                ),
                "retryable": True,
                "retry_scope": "CAPTURE_RGB_ONLY",
                "retry": {
                    "classification": "transient_observation",
                    "scope": "CAPTURE_RGB_ONLY",
                    "maximum_attempts": self.capture_attempts,
                    "backoff_s": self.capture_retry_backoff_s,
                    "requires_fresh_evidence": True,
                    "physical_action_submitted": False,
                },
                "retry_history": retry_history,
                "error": str(error),
                "physical_action_submitted": False,
                "capability_binding": binding,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _image_dimensions(captured: Any) -> tuple[int, int]:
        observation = getattr(captured, "observation", None)
        reference = observation.get("data") if isinstance(observation, dict) else None
        if isinstance(reference, dict):
            width = int(reference.get("width") or 0)
            height = int(reference.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        with Image.open(io.BytesIO(captured.image_bytes)) as image:
            return int(image.width), int(image.height)

    @classmethod
    def _parse_visual_result(
        cls,
        text: str,
        *,
        image_width: int | None = None,
        image_height: int | None = None,
        coordinate_hint: str | None = None,
    ) -> dict[str, Any]:
        decoded = cls._decode_json_object(text)
        if decoded is None:
            return {
                "answer": str(text).strip(),
                "confidence": "unknown",
                "annotations": [],
                "annotation_processing": cls._empty_annotation_processing(),
            }
        answer_value = decoded.get("answer")
        answer = (
            str(answer_value).strip()
            if isinstance(answer_value, str) and answer_value.strip()
            else str(text).strip()
        )
        raw_confidence = str(decoded.get("confidence") or "unknown").lower()
        confidence = (
            raw_confidence
            if raw_confidence in {"low", "medium", "high"}
            else "unknown"
        )
        annotations, annotation_processing = cls._sanitize_annotations(
            decoded.get("annotations"),
            declared_coordinate_space=decoded.get("coordinate_space"),
            image_width=image_width,
            image_height=image_height,
            coordinate_hint=coordinate_hint,
        )
        return {
            "answer": answer[:4000],
            "confidence": confidence,
            "annotations": annotations,
            "annotation_processing": annotation_processing,
        }

    @staticmethod
    def _decode_json_object(text: str) -> dict[str, Any] | None:
        candidate = str(text).strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            decoded = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    @classmethod
    def _sanitize_annotations(
        cls,
        value: Any,
        *,
        declared_coordinate_space: Any = None,
        image_width: int | None = None,
        image_height: int | None = None,
        coordinate_hint: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(value, list):
            return [], cls._empty_annotation_processing()
        annotations: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        coordinate_spaces: dict[str, int] = {}
        for index, item in enumerate(value[:24]):
            if not isinstance(item, dict):
                rejections.append(
                    {"index": index, "reason": "annotation_not_an_object"}
                )
                continue
            annotation_type = str(item.get("type") or "").lower()
            geometry, coordinate_space, rejection_reason = (
                cls._annotation_geometry(
                    annotation_type,
                    item,
                    declared_coordinate_space=declared_coordinate_space,
                    image_width=image_width,
                    image_height=image_height,
                    coordinate_hint=coordinate_hint,
                )
            )
            if geometry is None:
                rejections.append(
                    {
                        "index": index,
                        "type": annotation_type[:40],
                        "reason": rejection_reason or "invalid_geometry",
                    }
                )
                continue
            assert coordinate_space is not None
            coordinate_spaces[coordinate_space] = (
                coordinate_spaces.get(coordinate_space, 0) + 1
            )
            label = str(item.get("label") or annotation_type).strip()[:120]
            annotation_confidence = str(
                item.get("confidence") or "unknown"
            ).lower()
            if annotation_confidence not in {"low", "medium", "high"}:
                annotation_confidence = "unknown"
            annotations.append(
                {
                    "id": f"annotation-{index + 1}",
                    "type": annotation_type,
                    "label": label,
                    "confidence": annotation_confidence,
                    "applies_to_channels": ["rgb"],
                    **geometry,
                }
            )
        processed_count = min(len(value), 24)
        return annotations, {
            "input_count": len(value),
            "processed_count": processed_count,
            "accepted_count": len(annotations),
            "rejected_count": len(rejections),
            "truncated_count": max(0, len(value) - processed_count),
            "coordinate_spaces": coordinate_spaces,
            "rejections": rejections,
        }

    @classmethod
    def _annotation_geometry(
        cls,
        annotation_type: str,
        item: dict[str, Any],
        *,
        declared_coordinate_space: Any,
        image_width: int | None,
        image_height: int | None,
        coordinate_hint: str | None,
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        if annotation_type == "point":
            keys = ("x", "y")
        elif annotation_type == "box":
            keys = ("x", "y", "width", "height")
        else:
            return None, None, "unsupported_annotation_type"

        raw_geometry = {
            key: cls._finite_number(item.get(key)) for key in keys
        }
        if any(value is None for value in raw_geometry.values()):
            return None, None, "invalid_numeric_geometry"
        numeric_geometry = {
            key: float(value) for key, value in raw_geometry.items()
        }
        requested_coordinate_space = (
            item.get("coordinate_space")
            if item.get("coordinate_space") is not None
            else declared_coordinate_space
        )
        coordinate_space = cls._coordinate_space(
            requested_coordinate_space,
            numeric_geometry,
            coordinate_hint=coordinate_hint,
        )
        if coordinate_space is None:
            reason = (
                "missing_coordinate_space"
                if requested_coordinate_space is None
                else "unsupported_coordinate_space"
            )
            return None, None, reason
        normalized = cls._normalize_geometry(
            numeric_geometry,
            coordinate_space=coordinate_space,
            image_width=image_width,
            image_height=image_height,
        )
        if normalized is None:
            reason = (
                "image_dimensions_required"
                if coordinate_space == "pixels"
                and not cls._valid_image_dimensions(image_width, image_height)
                else "geometry_out_of_bounds"
            )
            return None, coordinate_space, reason
        if annotation_type == "box":
            width = normalized["width"]
            height = normalized["height"]
            if width <= 0.0 or height <= 0.0:
                return None, coordinate_space, "non_positive_box_size"
            if (
                normalized["x"] + width > 1.0
                or normalized["y"] + height > 1.0
            ):
                return None, coordinate_space, "geometry_out_of_bounds"
        return normalized, coordinate_space, None

    @classmethod
    def _coordinate_space(
        cls,
        requested: Any,
        geometry: dict[str, float],
        *,
        coordinate_hint: str | None,
    ) -> str | None:
        if requested is not None:
            return cls._canonical_coordinate_space(requested)
        if all(0.0 <= value <= 1.0 for value in geometry.values()):
            return "normalized_0_1"
        return cls._canonical_coordinate_space(coordinate_hint)

    @staticmethod
    def _canonical_coordinate_space(value: Any) -> str | None:
        candidate = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "normalized": "normalized_0_1",
            "normalized_0_1": "normalized_0_1",
            "unit": "normalized_0_1",
            "normalized_0_1000": "normalized_0_1000",
            "robotics_0_1000": "normalized_0_1000",
            "pixel": "pixels",
            "pixels": "pixels",
        }
        return aliases.get(candidate)

    @classmethod
    def _normalize_geometry(
        cls,
        geometry: dict[str, float],
        *,
        coordinate_space: str,
        image_width: int | None,
        image_height: int | None,
    ) -> dict[str, float] | None:
        if coordinate_space == "normalized_0_1":
            x_scale = y_scale = 1.0
        elif coordinate_space == "normalized_0_1000":
            x_scale = y_scale = 1000.0
        elif coordinate_space == "pixels":
            if not cls._valid_image_dimensions(image_width, image_height):
                return None
            assert image_width is not None and image_height is not None
            x_scale = float(image_width)
            y_scale = float(image_height)
        else:
            return None
        normalized = {
            key: value / (x_scale if key in {"x", "width"} else y_scale)
            for key, value in geometry.items()
        }
        if not all(0.0 <= value <= 1.0 for value in normalized.values()):
            return None
        return normalized

    @staticmethod
    def _valid_image_dimensions(
        image_width: int | None,
        image_height: int | None,
    ) -> bool:
        return (
            isinstance(image_width, int)
            and not isinstance(image_width, bool)
            and image_width > 0
            and isinstance(image_height, int)
            and not isinstance(image_height, bool)
            and image_height > 0
        )

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _empty_annotation_processing() -> dict[str, Any]:
        return {
            "input_count": 0,
            "processed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "truncated_count": 0,
            "coordinate_spaces": {},
            "rejections": [],
        }

    @staticmethod
    def _model_coordinate_hint(model_id: str) -> str | None:
        candidate = str(model_id).lower()
        if "gemini-robotics-er" in candidate:
            return "normalized_0_1000"
        return None

    @staticmethod
    def _evidence_title() -> str:
        return "Pointing identification"

    @staticmethod
    def _source_skill_id() -> str:
        return "test_agent.identify_pointed_object.v1"

    @staticmethod
    def _prompt(user_question: str) -> str:
        return f"""
Use only the supplied single RGB image. Do not assume depth, IMU, previous frames,
or hidden sensor data. A person may be pointing at an object in the scene.

User request: {user_question}

Identify the most likely object being pointed at. If the pointing gesture is
not visible or is ambiguous, say so directly and name up to two plausible
objects. Return only one JSON object with this shape:
{{
  "answer": "concise object label and visual reason",
  "confidence": "low|medium|high",
  "coordinate_space": "normalized_0_1000",
  "annotations": [
    {{"type":"point","x":0,"y":0,"label":"pointing fingertip","confidence":"low|medium|high"}},
    {{"type":"box","x":0,"y":0,"width":0,"height":0,"label":"target object","confidence":"low|medium|high"}}
  ]
}}
All x, y, width, and height values are integers from 0 through 1000, normalized
independently to image width and height. The origin is top-left, +X right, and
+Y down. Include only annotations supported by visible pixels. Omit the
fingertip point or target box when it cannot be localized. Do not claim that
the robot moved or interacted with anything.
""".strip()


class VisualSceneAnalysisSkill(PointingIdentificationSkill):
    """General current-frame VLM Skill using the same routed backend boundary."""

    @staticmethod
    def _prompt(user_question: str) -> str:
        return f"""
Use only the supplied single RGB image. Do not assume depth, IMU, previous
frames, world coordinates, or hidden sensor data.

User request: {user_question}

Answer the visual question directly. Separate visible evidence from inference
and state ambiguity. Return only one JSON object with this shape:
{{
  "answer": "concise evidence-based answer",
  "confidence": "low|medium|high",
  "coordinate_space": "normalized_0_1000",
  "annotations": [
    {{"type":"point","x":0,"y":0,"label":"visible feature","confidence":"low|medium|high"}},
    {{"type":"box","x":0,"y":0,"width":0,"height":0,"label":"visible region","confidence":"low|medium|high"}}
  ]
}}
All x, y, width, and height values are integers from 0 through 1000, normalized
independently to image width and height. The origin is top-left, +X right, and
+Y down. Use an empty annotation list when the answer cannot be localized to
visible pixels. Do not claim that the robot moved or interacted with anything.
""".strip()

    @staticmethod
    def _evidence_title() -> str:
        return "Visual scene analysis"

    @staticmethod
    def _source_skill_id() -> str:
        return "test_agent.visual_scene_analysis.v1"
