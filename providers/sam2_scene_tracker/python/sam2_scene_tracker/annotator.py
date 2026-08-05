from __future__ import annotations

import base64
import json
import time
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import cv2
import httpx
import numpy as np

from .policy import SceneSegmentationPolicy
from .prompts import ARM_OBJECT_ID, VisualPrompt, parse_visual_prompts


def _encode_jpeg(image_rgb: np.ndarray) -> bytes:
    ok, payload = cv2.imencode(
        ".jpg",
        cv2.cvtColor(np.asarray(image_rgb), cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, 90],
    )
    if not ok:
        raise RuntimeError("OpenCV could not encode scene annotation input")
    return payload.tobytes()


def _extract_openai_output_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
    text = "".join(texts).strip()
    if not text:
        raise RuntimeError("OpenAI scene annotator returned no output text")
    return text


def _extract_gemini_output_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        content = content if isinstance(content, dict) else {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    text = "".join(texts).strip()
    if not text:
        raise RuntimeError("Gemini scene annotator returned no output text")
    return text


def _schema(object_ids: list[str]) -> dict[str, Any]:
    coordinate = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
        "minItems": 2,
        "maxItems": 2,
    }
    return {
        "type": "object",
        "properties": {
            "detections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "object_id": {"type": "string", "enum": object_ids},
                        "region_id": {"type": "string"},
                        "box_2d": {
                            "type": "array",
                            "items": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 1000,
                            },
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "positive_points_2d": {
                            "type": "array",
                            "items": coordinate,
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": [
                        "object_id",
                        "region_id",
                        "box_2d",
                        "positive_points_2d",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["detections"],
        "additionalProperties": False,
    }


def _request(
    policy: SceneSegmentationPolicy,
) -> tuple[str, list[str], dict[str, Any]]:
    descriptions = [
        {
            "object_id": ARM_OBJECT_ID,
            "description": policy.arm_description,
            "role": "ARM_SELF_MASK",
        },
        *[
            {
                "object_id": value.object_id,
                "description": value.description,
                "role": value.object_type,
            }
            for value in policy.objects
        ],
    ]
    object_ids = [value["object_id"] for value in descriptions]
    prompt = (
        "Annotate only the listed objects in the live robot camera image. "
        "The descriptions are authoritative user/upstream labels; do not "
        "invent obstacle classes for anything else. Return one or more tight "
        "visible-region boxes per listed object when needed. For each region, "
        "return exactly two well-separated positive points safely inside that "
        "object and away from edges, holes, reflections, occluders, or other "
        "objects. Never use one loose enclosing box when it would contain a "
        "nearby non-target object; split a target into multiple tight regions "
        "instead. The ARM_SELF_MASK must cover the complete visible robot arm, "
        "including base, links, joints, cables, wrist, and gripper, using "
        "separate tight regions as needed, but must exclude the support table "
        "and every nearby workpiece. Use [y,x] coordinates normalized to "
        "integer 0-1000. "
        "Omit a listed object only if it is not visibly segmentable. Return only "
        "JSON matching the supplied schema.\n\nAUTHORITATIVE OBJECT LIST:\n"
        + json.dumps(descriptions, ensure_ascii=False)
    )
    return prompt, object_ids, _schema(object_ids)


def _quality_schema(object_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "accepted": {"type": "boolean"},
            "object_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "object_id": {"type": "string", "enum": object_ids},
                        "acceptable": {"type": "boolean"},
                        "problem": {
                            "type": "string",
                            "enum": [
                                "NONE",
                                "TARGET_MISSING",
                                "TARGET_SPILL",
                                "ARM_LEAK",
                                "OTHER_OBJECT_LEAK",
                                "BACKGROUND_LEAK",
                            ],
                        },
                    },
                    "required": ["object_id", "acceptable", "problem"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["accepted", "object_results"],
        "additionalProperties": False,
    }


def _quality_review_image(
    image_rgb: np.ndarray,
    depth_m: np.ndarray,
    masks: Mapping[str, np.ndarray],
    object_ids: list[str],
) -> tuple[np.ndarray, dict[str, str]]:
    image = np.ascontiguousarray(np.asarray(image_rgb, dtype=np.uint8))
    depth = np.asarray(depth_m, dtype=np.float32)
    if image.ndim != 3 or image.shape[2] != 3 or depth.shape != image.shape[:2]:
        raise ValueError("quality review RGB and depth shapes are incompatible")
    colors = [
        (255, 64, 64),
        (64, 220, 64),
        (64, 128, 255),
        (240, 200, 48),
        (220, 64, 220),
        (48, 220, 220),
    ]
    overlay = image.astype(np.float32)
    legend: dict[str, str] = {}
    for index, object_id in enumerate(object_ids):
        color = colors[index % len(colors)]
        legend[object_id] = f"rgb({color[0]},{color[1]},{color[2]})"
        selected = np.asarray(masks.get(object_id), dtype=bool)
        if selected.shape != image.shape[:2]:
            raise ValueError(f"quality review mask {object_id!r} has wrong shape")
        overlay[selected] = overlay[selected] * 0.35 + np.asarray(color) * 0.65
        cv2.putText(
            overlay,
            f"{index + 1}: {object_id}",
            (12, 30 + index * 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )
    valid = np.isfinite(depth) & (depth > 0.0)
    normalized = np.zeros(depth.shape, dtype=np.uint8)
    if np.any(valid):
        low, high = np.percentile(depth[valid], [2.0, 98.0])
        if float(high - low) <= 1e-6:
            normalized[valid] = 128
        else:
            normalized[valid] = np.clip(
                (depth[valid] - low) * 255.0 / float(high - low),
                0,
                255,
            ).astype(np.uint8)
    depth_bgr = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    depth_rgb = cv2.cvtColor(depth_bgr, cv2.COLOR_BGR2RGB)
    depth_rgb[~valid] = 0
    separator = np.full((image.shape[0], 8, 3), 255, dtype=np.uint8)
    return np.concatenate(
        [image, separator, np.clip(overlay, 0, 255).astype(np.uint8), separator, depth_rgb],
        axis=1,
    ), legend


def _quality_request(
    policy: SceneSegmentationPolicy,
    legend: Mapping[str, str],
) -> tuple[str, list[str], dict[str, Any]]:
    descriptions = [
        {
            "object_id": ARM_OBJECT_ID,
            "description": policy.arm_description,
            "role": "ARM_SELF_MASK",
        },
        *[
            {
                "object_id": value.object_id,
                "description": value.description,
                "role": value.object_type,
            }
            for value in policy.objects
        ],
    ]
    object_ids = [value["object_id"] for value in descriptions]
    prompt = (
        "Review segmentation quality for robot collision mapping. The image has "
        "three panels: original RGB, colored mask overlay, and registered depth. "
        "Use the authoritative descriptions and color legend below. Accept only "
        "when every colored mask covers its named visible object closely enough "
        "for collision geometry and does not spill onto the robot arm, another "
        "object, floor/background, or an unrelated surface. A small conservative "
        "boundary halo is acceptable. The arm self mask must cover visible robot "
        "parts and exclude support surfaces and workpieces. Return exactly one "
        "object_result for every listed object. `accepted` must be true only when "
        "all object results are acceptable. Return only schema-valid JSON.\n\n"
        "AUTHORITATIVE OBJECTS:\n"
        + json.dumps(descriptions, ensure_ascii=False)
        + "\n\nCOLOR LEGEND:\n"
        + json.dumps(dict(legend), ensure_ascii=False)
    )
    return prompt, object_ids, _quality_schema(object_ids)


class SceneVlmBackend(Protocol):
    backend_id: str
    model_id: str

    def generate(
        self,
        image_jpeg: bytes,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class GeminiSceneVlmBackend:
    backend_id = "google.gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_s: float = 45.0,
        http: httpx.Client | None = None,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("GEMINI_API_KEY is unavailable for scene annotation")
        self.api_key = key
        self.model_id = str(model or "").strip()
        if not self.model_id:
            raise ValueError("Gemini scene model is empty")
        self.http = http or httpx.Client(timeout=float(timeout_s))
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def generate(
        self,
        image_jpeg: bytes,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = self.http.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(self.model_id, safe='')}:generateContent",
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": base64.b64encode(image_jpeg).decode("ascii"),
                                }
                            },
                            {"text": prompt},
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "thinkingConfig": {"thinkingBudget": 0},
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            },
        )
        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(
                f"Gemini scene annotation failed ({response.status_code}): "
                f"{detail or response.reason_phrase}"
            )
        return json.loads(_extract_gemini_output_text(response.json()))


class OpenAISceneVlmBackend:
    backend_id = "openai.responses"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_s: float = 45.0,
        http: httpx.Client | None = None,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("OPENAI_API_KEY is unavailable for scene annotation")
        self.api_key = key
        self.model_id = str(model or "").strip()
        if not self.model_id:
            raise ValueError("OpenAI scene model is empty")
        self.http = http or httpx.Client(timeout=float(timeout_s))
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def generate(
        self,
        image_jpeg: bytes,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = base64.b64encode(image_jpeg).decode("ascii")
        response = self.http.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model_id,
                "reasoning": {"effort": "low"},
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{encoded}",
                                "detail": "original",
                            },
                        ],
                    }
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "midbrain_scene_annotations",
                        "strict": True,
                        "schema": schema,
                    }
                },
                "max_output_tokens": 4096,
                "store": False,
            },
        )
        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(
                f"OpenAI scene annotation failed ({response.status_code}): "
                f"{detail or response.reason_phrase}"
            )
        return json.loads(_extract_openai_output_text(response.json()))


class RoutedSceneAnnotator:
    """Configured model-pool routing; SAM2 remains the high-rate perception loop."""

    def __init__(self, backends: list[SceneVlmBackend]) -> None:
        if not backends:
            raise ValueError("at least one scene VLM backend must be configured")
        self.backends = list(backends)
        self.last_result: dict[str, Any] | None = None

    def close(self) -> None:
        for backend in self.backends:
            backend.close()

    def describe(self) -> dict[str, Any]:
        return {
            "ordered_candidates": [
                {"backend_id": value.backend_id, "model_id": value.model_id}
                for value in self.backends
            ],
            "last_result": self.last_result,
        }

    def annotate(
        self,
        image_rgb: np.ndarray,
        policy: SceneSegmentationPolicy,
    ) -> dict[str, list[VisualPrompt]]:
        image_jpeg = _encode_jpeg(image_rgb)
        prompt, object_ids, schema = _request(policy)
        failures: list[dict[str, str]] = []
        for backend in self.backends:
            started = time.monotonic()
            try:
                payload = backend.generate(image_jpeg, prompt, schema)
                result = parse_visual_prompts(
                    payload,
                    expected_object_ids=set(object_ids),
                )
                self.last_result = {
                    "backend_id": backend.backend_id,
                    "model_id": backend.model_id,
                    "elapsed_ms": (time.monotonic() - started) * 1000.0,
                    "failed_candidates": failures,
                }
                return result
            except Exception as error:
                failures.append(
                    {
                        "backend_id": backend.backend_id,
                        "model_id": backend.model_id,
                        "error": str(error),
                    }
                )
        self.last_result = {"failed_candidates": failures}
        raise RuntimeError(
            "all configured scene VLM candidates failed: "
            + "; ".join(
                f"{value['backend_id']}/{value['model_id']}: {value['error']}"
                for value in failures
            )
        )

    def validate_masks(
        self,
        image_rgb: np.ndarray,
        depth_m: np.ndarray,
        masks: Mapping[str, np.ndarray],
        policy: SceneSegmentationPolicy,
    ) -> dict[str, Any]:
        """Use one routed VLM to review SAM2 masks before persistent fusion."""

        object_ids = [ARM_OBJECT_ID, *[value.object_id for value in policy.objects]]
        review_image, legend = _quality_review_image(
            image_rgb,
            depth_m,
            masks,
            object_ids,
        )
        prompt, expected_ids, schema = _quality_request(policy, legend)
        image_jpeg = _encode_jpeg(review_image)
        failures: list[dict[str, str]] = []
        for backend in self.backends:
            started = time.monotonic()
            try:
                payload = backend.generate(image_jpeg, prompt, schema)
                raw_results = payload.get("object_results")
                if not isinstance(raw_results, list):
                    raise ValueError("mask review object_results must be an array")
                by_id: dict[str, dict[str, Any]] = {}
                for value in raw_results:
                    if not isinstance(value, dict):
                        raise ValueError("mask review result must be an object")
                    object_id = str(value.get("object_id") or "")
                    if object_id not in expected_ids or object_id in by_id:
                        raise ValueError("mask review object identities are invalid")
                    by_id[object_id] = value
                if set(by_id) != set(expected_ids):
                    raise ValueError("mask review did not cover every expected object")
                accepted = bool(payload.get("accepted")) and all(
                    value.get("acceptable") is True for value in by_id.values()
                )
                result = {
                    "accepted": accepted,
                    "object_results": [by_id[value] for value in expected_ids],
                    "backend_id": backend.backend_id,
                    "model_id": backend.model_id,
                    "elapsed_ms": (time.monotonic() - started) * 1000.0,
                    "failed_candidates": failures,
                }
                self.last_result = {"operation": "MASK_QUALITY_REVIEW", **result}
                return result
            except Exception as error:
                failures.append(
                    {
                        "backend_id": backend.backend_id,
                        "model_id": backend.model_id,
                        "error": str(error),
                    }
                )
        self.last_result = {
            "operation": "MASK_QUALITY_REVIEW",
            "failed_candidates": failures,
        }
        raise RuntimeError(
            "all configured scene VLM mask reviewers failed: "
            + "; ".join(
                f"{value['backend_id']}/{value['model_id']}: {value['error']}"
                for value in failures
            )
        )


def build_scene_annotator(
    config: Mapping[str, Any],
    environment: Mapping[str, str],
) -> RoutedSceneAnnotator:
    candidates = config.get("vlm_candidates")
    if not isinstance(candidates, list) or not candidates:
        candidates = [
            {
                "backend": "google.gemini",
                "model": "gemini-robotics-er-2-preview",
            },
            {"backend": "openai.responses", "model": "gpt-5.6-luna"},
        ]
    timeout_s = float(config.get("vlm_request_timeout_s", 45.0))
    backends: list[SceneVlmBackend] = []
    skipped: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"vlm_candidates[{index}] must be an object")
        backend = str(candidate.get("backend") or "").strip().lower()
        model_environment = str(candidate.get("model_env") or "").strip()
        environment_model = (
            environment.get(model_environment) if model_environment else None
        )
        model = str(environment_model or candidate.get("model") or "").strip()
        if not model:
            raise ValueError(f"vlm_candidates[{index}].model is empty")
        if backend == "google.gemini":
            key = str(environment.get("GEMINI_API_KEY") or "").strip()
            if not key:
                skipped.append(f"{backend}/{model}: GEMINI_API_KEY unavailable")
                continue
            backends.append(
                GeminiSceneVlmBackend(key, model=model, timeout_s=timeout_s)
            )
        elif backend == "openai.responses":
            key = str(environment.get("OPENAI_API_KEY") or "").strip()
            if not key:
                skipped.append(f"{backend}/{model}: OPENAI_API_KEY unavailable")
                continue
            backends.append(
                OpenAISceneVlmBackend(key, model=model, timeout_s=timeout_s)
            )
        else:
            raise ValueError(
                f"vlm_candidates[{index}] has unsupported backend {backend!r}"
            )
    if not backends:
        raise RuntimeError(
            "no configured scene VLM candidate is available: " + "; ".join(skipped)
        )
    return RoutedSceneAnnotator(backends)
