"""OpenAI Responses API detector for boxes and positive SAM points."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

import httpx
import cv2
import numpy as np

from .vlm_detection import Detection, TARGETS, parse_detection_payload


DEFAULT_MODEL = "gpt-5.6-luna"


def encode_jpeg(image_rgb: np.ndarray, quality: int = 90) -> bytes:
    ok, encoded = cv2.imencode(
        ".jpg",
        cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not ok:
        raise RuntimeError("OpenCV could not encode camera frame")
    return encoded.tobytes()


def _data_url(payload: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_output_text(payload: Mapping[str, Any]) -> str:
    texts: list[str] = []
    refusals: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text" and part.get("text"):
                texts.append(str(part["text"]))
            elif part.get("type") == "refusal" and part.get("refusal"):
                refusals.append(str(part["refusal"]))
    if refusals:
        raise RuntimeError("OpenAI refused the localization request: " + " ".join(refusals))
    text = "".join(texts).strip()
    if not text:
        raise RuntimeError("OpenAI response did not contain output text")
    return text


def parse_detections(text: str) -> dict[str, Detection]:
    return parse_detection_payload(json.loads(text), detector_id="openai")


def detection_schema() -> dict[str, Any]:
    point_schema = {
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
                        "model_id": {
                            "type": "string",
                            "enum": list(TARGETS),
                        },
                        "label": {"type": "string"},
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
                            "items": point_schema,
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "required": [
                        "model_id",
                        "label",
                        "box_2d",
                        "positive_points_2d",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["detections"],
        "additionalProperties": False,
    }


class OpenAIVisionDetector:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 120.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.http = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self.http.close()

    def detect(
        self,
        live_image_rgb: np.ndarray,
        cad_references: Mapping[str, bytes] | None = None,
    ) -> dict[str, Detection]:
        prompt = (
            "Locate two rigid reBot B601-DM parts in the LIVE CAMERA IMAGE: "
            "robot_arm_root is the stationary base/arm root, and "
            "robot_gripper_slider_support is the rigid slider support at the arm end. "
            "CAD atlases show geometry from multiple angles only; ignore their color, scale, "
            "pose, and background. Return a tight visible-object box for each visible target, "
            "not a box for the entire robot arm. Also return exactly two well-separated positive "
            "points per target. Every point must be safely inside opaque target material and far "
            "from silhouette edges, holes, gaps, reflections, attached arm links, and background. "
            "Use [y, x] coordinates normalized to integer 0-1000 for boxes and points."
        )
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
            {"type": "input_text", "text": "LIVE CAMERA IMAGE:"},
            {
                "type": "input_image",
                "image_url": _data_url(encode_jpeg(live_image_rgb), "image/jpeg"),
                "detail": "original",
            },
        ]
        for model_id, reference in (cad_references or {}).items():
            if model_id in TARGETS:
                content.extend(
                    [
                        {
                            "type": "input_text",
                            "text": f"CAD REFERENCE FOR {model_id} ({TARGETS[model_id]}):",
                        },
                        {
                            "type": "input_image",
                            "image_url": _data_url(reference, "image/png"),
                            "detail": "original",
                        },
                    ]
                )

        response = self.http.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "reasoning": {"effort": "low"},
                "input": [{"role": "user", "content": content}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "rebot_part_detections",
                        "strict": True,
                        "schema": detection_schema(),
                    }
                },
                "max_output_tokens": 2048,
                "store": False,
            },
        )
        if response.is_error:
            try:
                details = response.json().get("error", {}).get("message")
            except Exception:
                details = None
            raise RuntimeError(
                f"OpenAI request failed ({response.status_code}): "
                f"{details or response.reason_phrase}"
            )
        return parse_detections(extract_output_text(response.json()))
