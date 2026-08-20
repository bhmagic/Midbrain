from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
import os
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .openai_responses import request_structured_response
from .profile import ModelProfile, OrientationCandidate, file_sha256


@dataclass(frozen=True)
class OrientationSelection:
    candidate_id: str
    confidence: float
    rationale: str
    model: str
    response_id: str | None
    attempt_count: int = 1


class OrientationSelector(Protocol):
    def select(
        self,
        reference_paths: tuple[Path, ...],
        contact_sheet_path: Path,
        candidates: tuple[OrientationCandidate, ...],
    ) -> OrientationSelection: ...


@dataclass(frozen=True)
class SegmentationPrompt:
    box_yxyx: tuple[int, int, int, int]
    positive_points_yx: tuple[tuple[int, int], ...]
    confidence: float
    rationale: str
    model: str
    response_id: str | None
    attempt_count: int = 1
    negative_points_yx: tuple[tuple[int, int], ...] = ()


class PromptLocator(Protocol):
    def locate(
        self,
        reference_paths: tuple[Path, ...],
        scene_path: Path,
        *,
        attempt_index: int = 1,
        attempt_count: int = 1,
        additional_guidance: str = "",
    ) -> SegmentationPrompt: ...


def _intrinsics(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, dict):
        return float(value["fx"]), float(value["fy"]), float(value["cx"]), float(value["cy"])
    values = list(value)
    return float(values[0]), float(values[4]), float(values[2]), float(values[5])


def build_contact_sheet(
    rgb_path: Path,
    mask_path: Path,
    camera_from_centered_mesh: np.ndarray,
    camera_intrinsics: Any,
    profile: ModelProfile,
    output_path: Path,
    *,
    pre_orientation_correction: np.ndarray | None = None,
) -> Path:
    image = Image.open(rgb_path).convert("RGB")
    mask = np.asarray(Image.open(mask_path).convert("L")) > 0
    if mask.shape != (image.height, image.width):
        raise ValueError("orientation mask shape does not match RGB image")
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ValueError("orientation mask is empty")
    left, top, right, bottom = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    box_width = max(1, right - left + 1)
    box_height = max(1, bottom - top + 1)
    crop_box = (
        max(0, left - 2 * box_width),
        max(0, top - 2 * box_height),
        min(image.width, right + 2 * box_width),
        min(image.height, bottom + box_height),
    )
    panels: list[Image.Image] = []
    fx, fy, cx, cy = _intrinsics(camera_intrinsics)
    pre_correction = (
        np.eye(4, dtype=np.float64)
        if pre_orientation_correction is None
        else np.asarray(pre_orientation_correction, dtype=np.float64)
    )
    if pre_correction.shape != (4, 4):
        raise ValueError("pre-orientation correction must be a 4x4 matrix")
    for candidate in profile.candidates:
        panel = ImageOps.autocontrast(image, cutoff=1)
        draw = ImageDraw.Draw(panel)
        pose = (
            camera_from_centered_mesh
            @ pre_correction
            @ candidate.matrix
            @ profile.centered_mesh_from_arm_base
        )
        origin = pose[:3, 3]
        scale = 0.12
        points = [origin, origin + pose[:3, 0] * scale, origin + pose[:3, 1] * scale, origin + pose[:3, 2] * scale]
        projected: list[tuple[float, float]] = []
        for point in points:
            if point[2] <= 1e-6:
                projected.append((float(cx), float(cy)))
            else:
                projected.append((fx * point[0] / point[2] + cx, fy * point[1] / point[2] + cy))
        origin_px = projected[0]
        for endpoint, color, label in zip(projected[1:], ("red", "lime", "deepskyblue"), ("X", "Y", "Z")):
            draw.line([origin_px, endpoint], fill=color, width=8)
            draw.text(
                endpoint,
                label,
                fill=color,
                font=ImageFont.load_default(size=20),
                stroke_width=2,
                stroke_fill="black",
            )
        draw.rectangle([left, top, right, bottom], outline="yellow", width=5)
        crop = panel.crop(crop_box)
        crop.thumbnail((720, 540), Image.Resampling.LANCZOS)
        framed = Image.new("RGB", (740, 590), "#111827")
        framed.paste(crop, ((740 - crop.width) // 2, 42))
        ImageDraw.Draw(framed).text(
            (16, 14),
            f"{candidate.candidate_id}: local Z {candidate.degrees} deg",
            fill="white",
            font=ImageFont.load_default(size=18),
        )
        panels.append(framed)
    sheet = Image.new("RGB", (1480, 1180), "#030712")
    for index, panel in enumerate(panels):
        sheet.paste(panel, ((index % 2) * 740, (index // 2) * 590))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path


class OpenAIResponsesOrientationSelector:
    def __init__(
        self,
        model: str,
        timeout_s: float = 60.0,
        reasoning_effort: str = "low",
        backend: str = "openai.responses",
    ) -> None:
        self.backend = str(backend or "").strip().lower()
        self.key_name = (
            "GEMINI_API_KEY"
            if self.backend == "google.gemini"
            else "OPENAI_API_KEY"
        )
        self.key = str(os.environ.get(self.key_name) or "").strip()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.http = httpx.Client(timeout=float(timeout_s))

    @staticmethod
    def _image(path: Path) -> dict[str, str]:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "original"}

    def select(
        self,
        reference_paths: tuple[Path, ...],
        contact_sheet_path: Path,
        candidates: tuple[OrientationCandidate, ...],
    ) -> OrientationSelection:
        if not self.key:
            raise RuntimeError(
                f"{self.key_name} is unavailable for orientation selection"
            )
        candidate_ids = [value.candidate_id for value in candidates]
        schema = {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "enum": candidate_ids},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": ["candidate_id", "confidence", "rationale"],
            "additionalProperties": False,
        }
        prompt = (
            "The first image(s) are immutable robot reference views. Some references show "
            "only the base CAD; a full-arm axis reference intentionally has no interchangeable "
            "end effector. Ignore effector presence or absence and changing joint pose. Use the "
            "fixed base geometry, first arm mounting geometry, and labeled semantic axes only. "
            "The final image is a four-panel view of the observed robot. Each panel applies "
            "one allowed local-Z orientation to the same FoundationPose translation and draws "
            "the candidate semantic axes (X red, Y green, Z blue). Select only the panel whose "
            "axis orientation best matches the references. Do not invent a rotation, "
            "translation, or candidate outside the enum. Lower confidence when the images are ambiguous."
        )
        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        content.extend(self._image(path) for path in reference_paths)
        content.append(self._image(contact_sheet_path))
        structured = request_structured_response(
            self.http,
            backend=self.backend,
            key=self.key,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            content=content,
            schema_name="arm_base_orientation",
            schema=schema,
            operation="orientation-selection",
        )
        value = structured.value
        if value["candidate_id"] not in candidate_ids:
            raise RuntimeError("orientation VLM selected a candidate outside the profile")
        return OrientationSelection(
            candidate_id=value["candidate_id"],
            confidence=float(value["confidence"]),
            rationale=str(value["rationale"]),
            model=self.model,
            response_id=structured.response_id,
            attempt_count=structured.attempt_count,
        )

    def close(self) -> None:
        self.http.close()


class OpenAIResponsesArmBasePromptLocator:
    def __init__(
        self,
        model: str,
        timeout_s: float = 60.0,
        reasoning_effort: str = "low",
        backend: str = "openai.responses",
    ) -> None:
        self.backend = str(backend or "").strip().lower()
        self.key_name = (
            "GEMINI_API_KEY"
            if self.backend == "google.gemini"
            else "OPENAI_API_KEY"
        )
        self.key = str(os.environ.get(self.key_name) or "").strip()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.http = httpx.Client(timeout=float(timeout_s))

    def locate(
        self,
        reference_paths: tuple[Path, ...],
        scene_path: Path,
        *,
        attempt_index: int = 1,
        attempt_count: int = 1,
        additional_guidance: str = "",
    ) -> SegmentationPrompt:
        if not self.key:
            raise RuntimeError(
                f"{self.key_name} is unavailable for arm-base localization"
            )
        schema = {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "minimum": 0, "maximum": 1000},
                "left": {"type": "integer", "minimum": 0, "maximum": 1000},
                "bottom": {"type": "integer", "minimum": 0, "maximum": 1000},
                "right": {"type": "integer", "minimum": 0, "maximum": 1000},
                "point_1_y": {"type": "integer", "minimum": 0, "maximum": 1000},
                "point_1_x": {"type": "integer", "minimum": 0, "maximum": 1000},
                "negative_point_y": {"type": "integer", "minimum": 0, "maximum": 1000},
                "negative_point_x": {"type": "integer", "minimum": 0, "maximum": 1000},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": [
                "top",
                "left",
                "bottom",
                "right",
                "point_1_y",
                "point_1_x",
                "negative_point_y",
                "negative_point_x",
                "confidence",
                "rationale",
            ],
            "additionalProperties": False,
        }
        prompt = (
            f"This is independent mask-ensemble localization {attempt_index} of "
            f"{attempt_count}. Make this judgment independently from every other slot. "
            "The first image(s) are immutable robot references: a base CAD atlas and, when "
            "available, full-arm views whose interchangeable end effector is intentionally "
            "absent. The final image is the current camera scene. Use the full-arm reference "
            "to identify the fixed base assembly where the first arm link attaches. Return "
            "one tight axis-aligned box "
            "around only geometry that exists in the reference CAD, using integer coordinates "
            "normalized from 0 to 1000. The target is the profile-described base housing, first "
            "motor, and attached mounting geometry that exists in the reference CAD. Do not "
            "confuse a black target housing with an external black support enclosure. Explicitly "
            "stop the box at the "
            "lower edge of reference-CAD geometry. Exclude any black pedestal, riser, enclosure, "
            "illuminated sticker, tray, or table underneath it, even when that support touches "
            "the robot or resembles a cylinder. Also "
            "return one positive seed point well inside clearly visible reference-CAD base "
            "geometry; never put a positive point on the supporting pedestal. Across a "
            "mask ensemble the Skill will request this judgment independently multiple times, "
            "so do not widen the box to hedge ambiguity. Return one negative seed point "
            "clearly inside the excluded support "
            "enclosure immediately below the CAD/support seam; this point must not lie on the "
            "robot base joint. "
            "Do not include arm links, cables, the end effector, or unrelated hardware. Lower "
            "confidence when the CAD-defined base is occluded, absent, or ambiguous."
        )
        additional_guidance = str(additional_guidance or "").strip()
        if additional_guidance:
            prompt += (
                " Apply this arm-profile-specific target guidance; it overrides generic "
                "appearance assumptions above but does not permit unrelated support hardware: "
                f"{additional_guidance}"
            )
        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        content.extend(
            OpenAIResponsesOrientationSelector._image(path)
            for path in reference_paths
        )
        content.append(OpenAIResponsesOrientationSelector._image(scene_path))
        structured = request_structured_response(
            self.http,
            backend=self.backend,
            key=self.key,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            content=content,
            schema_name="arm_base_box",
            schema=schema,
            operation="arm-base localization",
        )
        value = structured.value
        box = tuple(int(value[field]) for field in ("top", "left", "bottom", "right"))
        points = (
            (int(value["point_1_y"]), int(value["point_1_x"])),
        )
        negative_points = (
            (int(value["negative_point_y"]), int(value["negative_point_x"])),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            raise RuntimeError("arm-base localization VLM returned an empty box")
        if any(
            not (box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3])
            for point in points
        ):
            raise RuntimeError("arm-base localization VLM seed point is outside its box")
        return SegmentationPrompt(
            box_yxyx=box,
            positive_points_yx=points,
            negative_points_yx=negative_points,
            confidence=float(value["confidence"]),
            rationale=str(value["rationale"]),
            model=self.model,
            response_id=structured.response_id,
            attempt_count=structured.attempt_count,
        )

    def close(self) -> None:
        self.http.close()


def orientation_evidence_hash(
    profile: ModelProfile, contact_sheet_path: Path, selection: OrientationSelection
) -> str:
    value = {
        "profile_sha256": profile.profile_sha256,
        "reference_set_sha256": profile.reference_set_sha256,
        "contact_sheet_sha256": file_sha256(contact_sheet_path),
        "candidate_id": selection.candidate_id,
        "confidence": selection.confidence,
        "model": selection.model,
        "response_id": selection.response_id,
    }
    return __import__("hashlib").sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
