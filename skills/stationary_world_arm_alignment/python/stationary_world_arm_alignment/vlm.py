from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping

import httpx
import numpy as np

from .camera import encode_rgb_jpeg


def _data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _schema() -> dict[str, Any]:
    point = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
        "minItems": 2,
        "maxItems": 2,
    }
    detection = {
        "type": "object",
        "properties": {
            "visible": {"type": "boolean"},
            "box_2d": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                "minItems": 4,
                "maxItems": 4,
            },
            "positive_points_2d": {
                "type": "array",
                "items": point,
                "minItems": 2,
                "maxItems": 2,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["visible", "box_2d", "positive_points_2d", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "base": detection,
            "gripper": detection,
            "jaw_state": {"type": "string", "enum": ["open", "closed", "uncertain"]},
            "beak_points_2d": {
                "type": "array",
                "items": point,
                "minItems": 1,
                "maxItems": 2,
            },
            "beak_faces_camera": {"type": "boolean"},
            "holding_object": {"type": "boolean"},
            "use_local_depth_minimum": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "base",
            "gripper",
            "jaw_state",
            "beak_points_2d",
            "beak_faces_camera",
            "holding_object",
            "use_local_depth_minimum",
            "notes",
        ],
        "additionalProperties": False,
    }


def _pose_validation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "pose_reasonable": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "box_fit": {"type": "string", "enum": ["GOOD", "ACCEPTABLE", "BAD"]},
            "orientation_fit": {
                "type": "string",
                "enum": ["GOOD", "ACCEPTABLE", "BAD"],
            },
            "matched_reference_view": {"type": "string"},
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
        },
        "required": [
            "pose_reasonable",
            "confidence",
            "box_fit",
            "orientation_fit",
            "matched_reference_view",
            "reasons",
        ],
        "additionalProperties": False,
    }


def extract_output_text(payload: Mapping[str, Any]) -> str:
    text: list[str] = []
    refusals: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text" and part.get("text"):
                text.append(str(part["text"]))
            if part.get("type") == "refusal" and part.get("refusal"):
                refusals.append(str(part["refusal"]))
    if refusals:
        raise RuntimeError("vision model refused localization: " + " ".join(refusals))
    if not text:
        raise RuntimeError("vision model returned no structured output")
    return "".join(text)


class GripperVision:
    def __init__(self, api_key: str, model: str, workspace_root: Path):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for gripper localization")
        self.api_key = api_key
        self.model = model
        self.workspace_root = workspace_root
        self.http = httpx.AsyncClient(timeout=180.0)

    def _reference_images(self) -> list[tuple[str, bytes]]:
        reference_root = (
            self.workspace_root
            / "providers"
            / "foundation_pose"
            / "defaults"
            / "rebot_b601_dm"
            / "references"
        )
        aliases = {
            "base": ["Base_reference_atlas.png", "Base_CAD_geometry.png"],
            "gripper": ["Gripper_reference_atlas.png", "Gripper_CAD_geometry.png"],
        }
        output: list[tuple[str, bytes]] = []
        for label, names in aliases.items():
            for name in names:
                path = reference_root / name
                if path.is_file():
                    output.append((label, path.read_bytes()))
                    break
        return output

    async def locate(self, rgb: np.ndarray, *, require_base: bool = True) -> dict[str, Any]:
        prompt = (
            "Analyze the LIVE RGB-D camera color image of a reBot B601-DM arm. Locate only the "
            "stationary base/root and rigid gripper slider support. Return tight visible-material "
            "boxes and two safe interior points for FoundationPose masks. Also pinpoint the foremost "
            "physical gripper beak tip. If the jaws are visibly open, return one tip per jaw and their "
            "mean will be used; otherwise return one tip. Coordinates are [y,x] integers from 0 to "
            "1000. Set use_local_depth_minimum true only when the beak faces the camera, is not holding "
            "anything, and should be locally closer than its surroundings. CAD images are geometry-only "
            "references; ignore their color, scale, pose, and background."
        )
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
            {"type": "input_text", "text": "LIVE CAMERA IMAGE:"},
            {
                "type": "input_image",
                "image_url": _data_url(encode_rgb_jpeg(rgb), "image/jpeg"),
                "detail": "original",
            },
        ]
        for label, payload in self._reference_images():
            content.extend(
                [
                    {"type": "input_text", "text": f"CAD REFERENCE FOR {label.upper()}:"},
                    {
                        "type": "input_image",
                        "image_url": _data_url(payload, "image/png"),
                        "detail": "original",
                    },
                ]
            )
        try:
            response = await self.http.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "reasoning": {"effort": "low"},
                    "input": [{"role": "user", "content": content}],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "stationary_world_arm_observations",
                            "strict": True,
                            "schema": _schema(),
                        }
                    },
                    "max_output_tokens": 2048,
                    "store": False,
                },
            )
        except httpx.ConnectError as error:
            raise RuntimeError(
                "OpenAI vision endpoint is unreachable; check outbound network access."
            ) from error
        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(
                f"OpenAI vision request failed ({response.status_code}): "
                f"{detail or response.reason_phrase}"
            )
        result = json.loads(extract_output_text(response.json()))
        if not result["gripper"]["visible"]:
            raise RuntimeError("the gripper must be visible for beak localization")
        if require_base and not result["base"]["visible"]:
            raise RuntimeError("the base must be visible for base alignment")
        return result

    async def validate_base_pose(
        self,
        overlay_jpeg: bytes,
        *,
        attempt: int,
    ) -> dict[str, Any]:
        references = dict(self._reference_images())
        base_reference = references.get("base")
        if base_reference is None:
            raise RuntimeError("the eight-angle FoundationPose base reference atlas is missing")
        prompt = (
            "Validate a FoundationPose estimate for the stationary reBot B601-DM base. "
            "The first image is the LIVE RGB image with the estimated base's projected 3D "
            "bounding box and XYZ arrows. The second image is an eight-angle CAD reference "
            "atlas of that base. Judge the 3D box and axes, not a segmentation mask. The box "
            "should enclose the visible physical base with plausible size, translation, "
            "perspective, and orientation matching one atlas view. Reject the estimate when "
            "the box is on the wrong object, floating or grossly offset, severely wrong in "
            "scale, mirrored, upside down, or has implausible axes. Minor occlusion and small "
            "projection error may be ACCEPTABLE. This is bounded attempt "
            f"{attempt} of 2."
        )
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
            {"type": "input_text", "text": "LIVE RGB WITH PROJECTED 3D BOX AND XYZ ARROWS:"},
            {
                "type": "input_image",
                "image_url": _data_url(overlay_jpeg, "image/jpeg"),
                "detail": "original",
            },
            {"type": "input_text", "text": "EIGHT-ANGLE BASE CAD REFERENCE ATLAS:"},
            {
                "type": "input_image",
                "image_url": _data_url(base_reference, "image/png"),
                "detail": "original",
            },
        ]
        try:
            response = await self.http.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "reasoning": {"effort": "low"},
                    "input": [{"role": "user", "content": content}],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "foundation_pose_base_validation",
                            "strict": True,
                            "schema": _pose_validation_schema(),
                        }
                    },
                    "max_output_tokens": 1024,
                    "store": False,
                },
            )
        except httpx.ConnectError as error:
            raise RuntimeError(
                "OpenAI vision endpoint is unreachable during base pose validation."
            ) from error
        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(
                f"OpenAI base pose validation failed ({response.status_code}): "
                f"{detail or response.reason_phrase}"
            )
        return json.loads(extract_output_text(response.json()))

    async def close(self) -> None:
        await self.http.aclose()
