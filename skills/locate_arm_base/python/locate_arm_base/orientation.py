from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .openai_responses import request_structured_response
from .profile import ModelProfile, file_sha256


@dataclass(frozen=True)
class EffectorPointObservation:
    identified: bool
    points_yx_0_1000: tuple[tuple[str, int, int], ...]
    confidence: float
    rationale: str
    model: str
    response_id: str | None
    attempt_count: int = 1


class EffectorPointLocator(Protocol):
    def locate(
        self,
        mounted_effector: dict[str, Any],
        scene_path: Path,
    ) -> EffectorPointObservation: ...


@dataclass(frozen=True)
class EffectorLandmarkSpec:
    landmark_id: str
    display_name: str
    point_ids: tuple[str, ...]
    description_for_vlm: str
    controlled_frame_id: str
    arm_base_frame: str
    controlled_frame_to_landmark_translation_m: tuple[float, float, float]
    source: str


@dataclass(frozen=True)
class GeometricOrientationResolution:
    candidate_id: str
    decision_basis: str
    candidate_comparisons: tuple[dict[str, Any], ...]
    selected_score: float
    runner_up_separation: float | None


def effector_landmark_spec(
    mounted_effector: dict[str, Any],
) -> EffectorLandmarkSpec:
    if not isinstance(mounted_effector, dict):
        raise ValueError("mounted effector profile must be an object")
    controlled_frame = mounted_effector.get("controlled_frame")
    controlled_frame = controlled_frame if isinstance(controlled_frame, dict) else {}
    controlled_frame_id = str(controlled_frame.get("frame_id") or "").strip()
    if not controlled_frame_id:
        raise RuntimeError("active mounted effector has no controlled frame")
    extensions = mounted_effector.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    orientation = extensions.get("midbrain.skill.locate_arm_base.v1")
    orientation = orientation if isinstance(orientation, dict) else {}
    arm_base_frame = str(
        orientation.get("arm_base_frame") or "rebot_arm_base"
    ).strip()
    selected = orientation.get("landmark")
    selected = selected if isinstance(selected, dict) else None
    if selected is None:
        display_name = str(
            mounted_effector.get("display_name")
            or mounted_effector.get("profile_id")
            or "mounted robot end effector"
        ).strip()
        return EffectorLandmarkSpec(
            landmark_id="coarse_effector_center",
            display_name=display_name,
            point_ids=("effector_center",),
            description_for_vlm=(
                f"Locate any clearly visible point on the active {display_name}. "
                "Prefer a rigid point near the center of the mounted effector."
            ),
            controlled_frame_id=controlled_frame_id,
            arm_base_frame=arm_base_frame,
            controlled_frame_to_landmark_translation_m=(0.0, 0.0, 0.0),
            source="GENERIC_MOUNTED_EFFECTOR_FALLBACK",
        )
    point_ids = tuple(
        str(value).strip()
        for value in selected.get("eligible_point_ids") or []
        if str(value).strip()
    )
    if not point_ids:
        point_ids = ("effector_center",)
    offset = selected.get("controlled_frame_to_landmark_translation_m")
    try:
        offset_values = tuple(float(value) for value in offset)
    except (TypeError, ValueError):
        offset_values = (0.0, 0.0, 0.0)
    if len(offset_values) != 3 or not all(math.isfinite(value) for value in offset_values):
        offset_values = (0.0, 0.0, 0.0)
    return EffectorLandmarkSpec(
        landmark_id=str(selected.get("landmark_id") or "coarse_effector_center"),
        display_name=str(
            selected.get("display_name")
            or mounted_effector.get("display_name")
            or "mounted robot end effector"
        ),
        point_ids=point_ids,
        description_for_vlm=str(
            selected.get("description_for_vlm")
            or "Locate any clearly visible point on the mounted robot end effector."
        ),
        controlled_frame_id=controlled_frame_id,
        arm_base_frame=arm_base_frame,
        controlled_frame_to_landmark_translation_m=offset_values,
        source="MOUNTED_EFFECTOR_LOCATE_ARM_BASE_EXTENSION",
    )


def _project_normalized_yx(
    camera_point: np.ndarray,
    camera_intrinsics: Any,
    image_size: tuple[int, int],
) -> tuple[float, float] | None:
    point = np.asarray(camera_point, dtype=np.float64)
    if point.shape != (3,) or not np.all(np.isfinite(point)) or point[2] <= 1e-6:
        return None
    width, height = (int(image_size[0]), int(image_size[1]))
    if width < 2 or height < 2:
        raise ValueError("orientation image dimensions must be at least 2x2")
    fx, fy, cx, cy = _intrinsics(camera_intrinsics)
    pixel_x = fx * point[0] / point[2] + cx
    pixel_y = fy * point[1] / point[2] + cy
    return (
        1000.0 * pixel_y / float(height - 1),
        1000.0 * pixel_x / float(width - 1),
    )


def resolve_orientation_from_effector_fk(
    *,
    observation: EffectorPointObservation,
    camera_from_centered_mesh: np.ndarray,
    upright_correction: np.ndarray,
    profile: ModelProfile,
    base_from_controlled_frame: np.ndarray,
    landmark_spec: EffectorLandmarkSpec,
    camera_intrinsics: Any,
    image_size: tuple[int, int],
) -> GeometricOrientationResolution:
    if not observation.identified or not observation.points_yx_0_1000:
        raise ValueError("effector point observation contains no recognized point")
    observed_y = sum(value[1] for value in observation.points_yx_0_1000) / len(
        observation.points_yx_0_1000
    )
    observed_x = sum(value[2] for value in observation.points_yx_0_1000) / len(
        observation.points_yx_0_1000
    )
    base_from_controlled = np.asarray(base_from_controlled_frame, dtype=np.float64)
    if base_from_controlled.shape != (4, 4):
        raise ValueError("base_from_controlled_frame must be a 4x4 matrix")
    landmark_in_controlled = np.asarray(
        [*landmark_spec.controlled_frame_to_landmark_translation_m, 1.0],
        dtype=np.float64,
    )
    landmark_in_base = base_from_controlled @ landmark_in_controlled
    comparisons: list[dict[str, Any]] = []
    for candidate in profile.candidates:
        camera_from_base = (
            camera_from_centered_mesh
            @ upright_correction
            @ candidate.matrix
            @ profile.centered_mesh_from_arm_base
        )
        camera_point = (camera_from_base @ landmark_in_base)[:3]
        projected = _project_normalized_yx(
            camera_point,
            camera_intrinsics,
            image_size,
        )
        if projected is None:
            raise RuntimeError(
                "timestamped FK effector point is not projectable for profiled "
                f"orientation candidate {candidate.candidate_id}"
            )
        distance = math.hypot(projected[0] - observed_y, projected[1] - observed_x)
        comparisons.append(
            {
                "candidate_id": candidate.candidate_id,
                "degrees": candidate.degrees,
                "predicted_yx_0_1000": [projected[0], projected[1]],
                "observed_mean_yx_0_1000": [observed_y, observed_x],
                "distance_0_1000": distance,
                "predicted_camera_point_m": camera_point.tolist(),
            }
        )
    comparisons.sort(key=lambda value: (value["distance_0_1000"], value["candidate_id"]))
    runner_up_separation = (
        float(comparisons[1]["distance_0_1000"] - comparisons[0]["distance_0_1000"])
        if len(comparisons) > 1
        else None
    )
    return GeometricOrientationResolution(
        candidate_id=str(comparisons[0]["candidate_id"]),
        decision_basis="SINGLE_VLM_EFFECTOR_POINT_WITH_TIMESTAMPED_FK",
        candidate_comparisons=tuple(comparisons),
        selected_score=float(comparisons[0]["distance_0_1000"]),
        runner_up_separation=runner_up_separation,
    )


def resolve_orientation_from_world_x_hint(
    *,
    rough_positive_x_world: Any,
    world_from_camera: np.ndarray,
    camera_from_centered_mesh: np.ndarray,
    upright_correction: np.ndarray,
    profile: ModelProfile,
) -> GeometricOrientationResolution:
    hint = np.asarray(rough_positive_x_world, dtype=np.float64)
    if hint.shape != (3,) or not np.all(np.isfinite(hint)):
        raise ValueError("rough_arm_base_positive_x_world must contain three finite values")
    norm = float(np.linalg.norm(hint))
    if norm < 1e-9:
        raise ValueError("rough_arm_base_positive_x_world must not be the zero vector")
    hint /= norm
    comparisons: list[dict[str, Any]] = []
    for candidate in profile.candidates:
        camera_from_base = (
            camera_from_centered_mesh
            @ upright_correction
            @ candidate.matrix
            @ profile.centered_mesh_from_arm_base
        )
        world_from_base = world_from_camera @ camera_from_base
        predicted_x_world = world_from_base[:3, 0]
        score = float(np.clip(np.dot(predicted_x_world, hint), -1.0, 1.0))
        comparisons.append(
            {
                "candidate_id": candidate.candidate_id,
                "degrees": candidate.degrees,
                "predicted_positive_x_world": predicted_x_world.tolist(),
                "rough_positive_x_world": hint.tolist(),
                "direction_dot": score,
            }
        )
    comparisons.sort(key=lambda value: (-value["direction_dot"], value["candidate_id"]))
    runner_up_separation = (
        float(comparisons[0]["direction_dot"] - comparisons[1]["direction_dot"])
        if len(comparisons) > 1
        else None
    )
    return GeometricOrientationResolution(
        candidate_id=str(comparisons[0]["candidate_id"]),
        decision_basis="AGENT_SUPPLIED_ROUGH_WORLD_POSITIVE_X",
        candidate_comparisons=tuple(comparisons),
        selected_score=float(comparisons[0]["direction_dot"]),
        runner_up_separation=runner_up_separation,
    )


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


def _input_image(path: Path) -> dict[str, str]:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime};base64,{encoded}",
        "detail": "original",
    }


class OpenAIResponsesEffectorPointLocator:
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
        mounted_effector: dict[str, Any],
        scene_path: Path,
    ) -> EffectorPointObservation:
        if not self.key:
            raise RuntimeError(
                f"{self.key_name} is unavailable for effector point localization"
            )
        spec = effector_landmark_spec(mounted_effector)
        schema = {
            "type": "object",
            "properties": {
                "effector_identified": {"type": "boolean"},
                "points": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": len(spec.point_ids),
                    "items": {
                        "type": "object",
                        "properties": {
                            "point_id": {"type": "string", "enum": list(spec.point_ids)},
                            "y_0_1000": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "x_0_1000": {"type": "integer", "minimum": 0, "maximum": 1000},
                        },
                        "required": ["point_id", "y_0_1000", "x_0_1000"],
                        "additionalProperties": False,
                    },
                },
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": [
                "effector_identified",
                "points",
                "confidence",
                "rationale",
            ],
            "additionalProperties": False,
        }
        point_names = ", ".join(spec.point_ids)
        prompt = (
            "The image is the current robot scene. Identify the active mounted robot "
            f"effector using this profile description: {spec.description_for_vlm} "
            f"The eligible named visual points are: {point_names}. This observation is "
            "used only for coarse 0/90/180/270-degree arm-base orientation resolution "
            "against timestamped forward kinematics. It is not a translation calibration "
            "or point-quality review. If the effector is identifiable, return one or more "
            "visible eligible points; one recognized point is sufficient, and you must not "
            "reject an otherwise recognizable point for imperfect depth, exact surface "
            "quality, partial occlusion, or low geometric precision. Coordinates are Y then "
            "X normalized from 0 to 1000 over the current image. Do not mark the robot base, "
            "an arm link, a work object, or background as the effector. If no eligible part "
            "of the active effector can be identified, set effector_identified=false and "
            "return an empty points array. Do not invent a point."
        )
        content: list[dict[str, str]] = [
            {"type": "input_text", "text": prompt},
            _input_image(scene_path),
        ]
        structured = request_structured_response(
            self.http,
            backend=self.backend,
            key=self.key,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            content=content,
            schema_name="arm_effector_coarse_points",
            schema=schema,
            operation="coarse effector point localization",
            maximum_attempts=1,
        )
        value = structured.value
        identified = bool(value.get("effector_identified"))
        points: list[tuple[str, int, int]] = []
        observed_ids: set[str] = set()
        for item in value.get("points") or []:
            if not isinstance(item, dict):
                continue
            point_id = str(item.get("point_id") or "")
            if point_id not in spec.point_ids or point_id in observed_ids:
                continue
            points.append(
                (point_id, int(item["y_0_1000"]), int(item["x_0_1000"]))
            )
            observed_ids.add(point_id)
        if not identified:
            points = []
        return EffectorPointObservation(
            identified=identified and bool(points),
            points_yx_0_1000=tuple(points),
            confidence=float(value.get("confidence") or 0.0),
            rationale=str(value.get("rationale") or ""),
            model=self.model,
            response_id=structured.response_id,
            attempt_count=structured.attempt_count,
        )

    def close(self) -> None:
        self.http.close()


def build_effector_orientation_overlay(
    *,
    rgb_path: Path,
    observation: EffectorPointObservation | None,
    resolution: GeometricOrientationResolution,
    output_path: Path,
) -> Path:
    image = Image.open(rgb_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)

    def pixel(y: float, x: float) -> tuple[float, float]:
        return (
            float(x) * float(image.width - 1) / 1000.0,
            float(y) * float(image.height - 1) / 1000.0,
        )

    if observation is not None:
        for point_id, y, x in observation.points_yx_0_1000:
            px, py = pixel(float(y), float(x))
            draw.ellipse([px - 9, py - 9, px + 9, py + 9], outline="yellow", width=4)
            draw.text((px + 12, py - 12), point_id, fill="yellow", font=font)
    colors = ("lime", "cyan", "magenta", "orange", "white", "deepskyblue")
    for index, comparison in enumerate(resolution.candidate_comparisons):
        projected = comparison.get("predicted_yx_0_1000")
        if not isinstance(projected, list) or len(projected) != 2:
            continue
        px, py = pixel(float(projected[0]), float(projected[1]))
        selected = comparison.get("candidate_id") == resolution.candidate_id
        color = "red" if selected else colors[index % len(colors)]
        radius = 11 if selected else 7
        draw.rectangle(
            [px - radius, py - radius, px + radius, py + radius],
            outline=color,
            width=5 if selected else 3,
        )
        draw.text(
            (px + radius + 4, py + radius + 2),
            str(comparison.get("candidate_id") or "candidate"),
            fill=color,
            font=font,
        )
    draw.rectangle([0, 0, image.width, 34], fill="#111827")
    draw.text(
        (10, 8),
        f"orientation {resolution.candidate_id} via {resolution.decision_basis}",
        fill="white",
        font=font,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


def effector_orientation_evidence_hash(
    *,
    profile: ModelProfile,
    rgb_path: Path,
    mounted_effector: dict[str, Any],
    observation: EffectorPointObservation | None,
    resolution: GeometricOrientationResolution,
) -> str:
    value = {
        "profile_sha256": profile.profile_sha256,
        "rgb_sha256": file_sha256(rgb_path),
        "mounted_effector_id": mounted_effector.get("profile_id"),
        "mounted_effector_revision": mounted_effector.get("profile_revision"),
        "observation": (
            None
            if observation is None
            else {
                "identified": observation.identified,
                "points_yx_0_1000": observation.points_yx_0_1000,
                "model": observation.model,
                "response_id": observation.response_id,
            }
        ),
        "candidate_id": resolution.candidate_id,
        "decision_basis": resolution.decision_basis,
        "candidate_comparisons": resolution.candidate_comparisons,
    }
    return __import__("hashlib").sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
        content.extend(_input_image(path) for path in reference_paths)
        content.append(_input_image(scene_path))
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
