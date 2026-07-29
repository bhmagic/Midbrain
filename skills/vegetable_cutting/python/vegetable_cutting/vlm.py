from __future__ import annotations

import base64
import json
from typing import Any, Mapping

import cv2
import httpx
import numpy as np

from .camera import encode_rgb_jpeg


def _data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def scene_schema() -> dict[str, Any]:
    point = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
        "minItems": 2,
        "maxItems": 2,
    }
    polygon = {
        "type": "array",
        "items": point,
        "minItems": 4,
        "maxItems": 24,
    }
    return {
        "type": "object",
        "properties": {
            "board": {
                "type": "object",
                "properties": {
                    "visible": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["visible", "confidence"],
                "additionalProperties": False,
            },
            "vegetable": {
                "type": "object",
                "properties": {
                    "visible": {"type": "boolean"},
                    "polygon_yx_1000": polygon,
                    "major_axis_endpoints_yx_1000": {
                        "type": "array",
                        "items": point,
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "cutting_line_board_endpoints_yx_1000": {
                        "type": "array",
                        "items": point,
                        "minItems": 2,
                        "maxItems": 2,
                        "description": (
                            "Left and right points on visible board surface, each "
                            "approximately 5 mm beyond the corresponding vegetable "
                            "end along its long axis."
                        ),
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "visible",
                    "polygon_yx_1000",
                    "major_axis_endpoints_yx_1000",
                    "cutting_line_board_endpoints_yx_1000",
                    "confidence",
                ],
                "additionalProperties": False,
            },
            "person_visible_in_workspace": {"type": "boolean"},
            "person_or_animal_visible_in_workspace": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "board",
            "vegetable",
            "person_visible_in_workspace",
            "person_or_animal_visible_in_workspace",
            "notes",
        ],
        "additionalProperties": False,
    }


def first_cut_alignment_schema() -> dict[str, Any]:
    point = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
        "minItems": 2,
        "maxItems": 2,
    }
    return {
        "type": "object",
        "properties": {
            "blade_and_target_visible": {"type": "boolean"},
            "depth_evidence_used": {"type": "boolean"},
            "depth_alignment_meaningful": {"type": "boolean"},
            "orange_cut_target_matches_vegetable": {"type": "boolean"},
            "blade_controlled_point_yx_1000": point,
            "person_or_animal_visible_in_workspace": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "string"},
        },
        "required": [
            "blade_and_target_visible",
            "depth_evidence_used",
            "depth_alignment_meaningful",
            "orange_cut_target_matches_vegetable",
            "blade_controlled_point_yx_1000",
            "person_or_animal_visible_in_workspace",
            "confidence",
            "notes",
        ],
        "additionalProperties": False,
    }


def workspace_presence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "person_or_animal_visible_in_workspace": {"type": "boolean"},
            "visible_subject_description": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "string"},
        },
        "required": [
            "person_or_animal_visible_in_workspace",
            "visible_subject_description",
            "confidence",
            "notes",
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
        raise RuntimeError("vision model refused scene localization: " + " ".join(refusals))
    if not text:
        raise RuntimeError("vision model returned no structured scene output")
    return "".join(text)


class SceneVision:
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for scene localization")
        self.api_key = api_key
        self.model = model
        self.http = httpx.AsyncClient(timeout=180.0)

    async def locate(self, rgb: np.ndarray) -> dict[str, Any]:
        prompt = (
            "Analyze this live RGB-D camera color image for a robot vegetable-cutting setup. "
            "Confirm that a cutting-board surface is visible; do not trace or reason from its "
            "outline because the board may have any shape. Identify the vegetable visible outline "
            "and long-axis endpoints. Also return exactly two cutting-line board points: one just "
            "beyond the left vegetable end and one just beyond the right vegetable end, each "
            "approximately 5 mm outside the vegetable along its long axis and visibly on the "
            "board surface. Do not localize the knife tip, heel, spine, or blade-handle junction; "
            "the controlled cutting point is provided by a configured hard-mount tool transform. "
            "Use tight visible-material boundaries. Coordinates are [y,x] integers from 0 to 1000. "
            "Mark person_visible_in_workspace true if any person or hand enters the board or robot "
            "work area. Mark person_or_animal_visible_in_workspace true if any person, hand, or "
            "animal enters that work area. A cat tree, furniture, clothing, boxes, the robot, the "
            "knife, shadows, and reflections are not people or animals. Do not propose robot "
            "motion, cutting depth, force, or safety decisions."
        )
        return await self._structured_image_response(
            rgb,
            prompt=prompt,
            schema_name="vegetable_cutting_scene",
            schema=scene_schema(),
        )

    async def assess_first_cut_alignment(
        self,
        rgb_with_target_overlay: np.ndarray,
        depth_with_target_overlay: np.ndarray,
        *,
        depth_near_m: float,
        depth_far_m: float,
        target_depth_m: float,
    ) -> dict[str, Any]:
        prompt = (
            "Analyze two pixel-registered images of a robot vegetable-cutting "
            "first-cut review. Image 1 is RGB with two overlays. The orange line "
            "is the intended cut on the board plane and should visibly cross the "
            "vegetable near its first end. The blue line is the expected blade "
            "location at the configured safe review height. Do not judge whether "
            "the physical blade already matches that blue line and do not estimate "
            "a motion vector. Instead, localize the physical controlled cutting "
            "point as the middle of the blade cutting edge, approximately 18 cm "
            "forward from the gripper; never use the knife tip. Return this one "
            "point as [y,x] integers from 0 to 1000. Deterministic geometry will "
            "compare that observed point with the exact blue target and compute "
            "the correction. This prevents a target drawn from an inaccurate "
            "absolute transform from certifying itself. The angled camera creates "
            "parallax, so the blue review-height line is expected to be visibly "
            "displaced from the orange board line and the vegetable. Do not move "
            "the blade onto the orange line in the image. Report separately "
            "whether the orange board-plane line still crosses the vegetable at "
            "a meaningful first cut. Image 2 is aligned metric depth with "
            "the same overlays. In "
            "Image 2, TURBO colors run from near "
            f"{depth_near_m:.4f} m to far {depth_far_m:.4f} m; black pixels are "
            "invalid. The blue review-height target is at camera depth "
            f"{target_depth_m:.4f} m. Use both images. Explicitly distinguish "
            "image-plane alignment from depth alignment and set "
            "depth_evidence_used true only if Image 2 informed the result. The "
            "metal blade can be reflective; when its depth is invalid, use the "
            "non-reflective handle and the blade-handle junction as a depth proxy "
            "for recognizing the blade/handle relationship. The blade point itself "
            "may use RGB because metal depth is unreliable. Do not return camera "
            "or arm axes, millimeters, an orientation change, or a judgment that "
            "the blade is already aligned. Confidence rates localization quality. "
            "Mark whether both blade and target are visible and "
            "whether any person, hand, or animal is in the robot workspace. Do "
            "not classify a cat tree, furniture, clothing, boxes, the robot, the "
            "knife, shadows, or reflections as a person or animal. "
            "Do not propose force, controller settings, cutting depth, or safety "
            "overrides. The correction is bounded by software and the human "
            "operator has final authority."
        )
        return await self._structured_image_response(
            rgb_with_target_overlay,
            prompt=prompt,
            schema_name="vegetable_cutting_first_cut_alignment",
            schema=first_cut_alignment_schema(),
            additional_images=[depth_with_target_overlay],
        )

    async def recheck_workspace_presence(
        self,
        rgb: np.ndarray,
    ) -> dict[str, Any]:
        prompt = (
            "This is a focused verification of a possible person-or-animal "
            "alert in a fixed robot vegetable-cutting workspace. Inspect the "
            "full RGB image. Set person_or_animal_visible_in_workspace true "
            "only when visible human or animal anatomy, such as a hand, arm, "
            "face, body, furred body, paw, or tail, enters the cutting board, "
            "table work area, or robot swept volume. A cat tree, furniture, "
            "clothing, boxes, the robot arm, gripper, knife, vegetable, "
            "shadows, printed images, and reflections are not people or "
            "animals. Describe the actual visible subject that caused a true "
            "result. This call only verifies visual presence and must not "
            "propose motion or override the operator."
        )
        return await self._structured_image_response(
            rgb,
            prompt=prompt,
            schema_name="vegetable_cutting_workspace_presence_recheck",
            schema=workspace_presence_schema(),
        )

    async def _structured_image_response(
        self,
        rgb: np.ndarray,
        *,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        additional_images: list[np.ndarray] | None = None,
    ) -> dict[str, Any]:
        images = [rgb, *(additional_images or [])]
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt}
        ]
        content.extend(
            {
                "type": "input_image",
                "image_url": _data_url(encode_rgb_jpeg(image), "image/jpeg"),
                "detail": "original",
            }
            for image in images
        )
        response = await self.http.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "reasoning": {"effort": "low"},
                "input": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
                "max_output_tokens": 2048,
                "store": False,
            },
        )
        response.raise_for_status()
        return json.loads(extract_output_text(response.json()))

    async def close(self) -> None:
        await self.http.aclose()


def render_registered_depth_evidence(
    depth_m: np.ndarray,
) -> tuple[np.ndarray, dict[str, float]]:
    valid_mask = np.isfinite(depth_m) & (depth_m > 0)
    valid = depth_m[valid_mask]
    if not valid.size:
        return np.zeros((*depth_m.shape, 3), dtype=np.uint8), {
            "near_m": 0.0,
            "far_m": 0.0,
            "valid_fraction": 0.0,
        }
    near_m = float(np.percentile(valid, 2))
    far_m = float(np.percentile(valid, 98))
    span_m = max(1e-6, far_m - near_m)
    normalized = np.clip((depth_m - near_m) / span_m, 0.0, 1.0)
    depth_u8 = np.rint(normalized * 255.0).astype(np.uint8)
    bgr = cv2.applyColorMap(depth_u8, cv2.COLORMAP_TURBO)
    bgr[~valid_mask] = 0
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return rgb, {
        "near_m": near_m,
        "far_m": far_m,
        "valid_fraction": float(np.mean(valid_mask)),
    }
