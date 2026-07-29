from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

import httpx
import numpy as np

from .camera import encode_rgb_jpeg


OPENAI_API_ROUTE = "OPENAI_API"
REVIEWED_FILE_ROUTE = "REVIEWED_FILE"


def _data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256(encoded)


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


def _validate_point(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or any(item < 0 or item > 1000 for item in value)
    ):
        raise RuntimeError(f"{label} must be two integer [y,x] values from 0 to 1000")
    return value


def _validate_detection(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} detection must be an object")
    required = {"visible", "box_2d", "positive_points_2d", "confidence"}
    if set(value) != required:
        raise RuntimeError(f"{label} detection fields do not match the reviewed schema")
    if not isinstance(value["visible"], bool):
        raise RuntimeError(f"{label}.visible must be boolean")
    box = value["box_2d"]
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in box)
        or any(item < 0 or item > 1000 for item in box)
    ):
        raise RuntimeError(f"{label}.box_2d must contain four integers from 0 to 1000")
    points = value["positive_points_2d"]
    if not isinstance(points, list) or len(points) != 2:
        raise RuntimeError(f"{label}.positive_points_2d must contain exactly two points")
    for index, point in enumerate(points):
        _validate_point(point, f"{label}.positive_points_2d[{index}]")
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise RuntimeError(f"{label}.confidence must be between 0 and 1")
    return value


def validate_localization_result(
    value: Any,
    *,
    require_base: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_schema()["required"]):
        raise RuntimeError("reviewed localization fields do not match the required schema")
    base = _validate_detection(value["base"], "base")
    gripper = _validate_detection(value["gripper"], "gripper")
    if require_base and not base["visible"]:
        raise RuntimeError("the reviewed localization must include a visible base")
    if not gripper["visible"]:
        raise RuntimeError("the reviewed localization must include a visible gripper")
    if value["jaw_state"] not in {"open", "closed", "uncertain"}:
        raise RuntimeError("jaw_state is invalid")
    beak_points = value["beak_points_2d"]
    if not isinstance(beak_points, list) or not 1 <= len(beak_points) <= 2:
        raise RuntimeError("beak_points_2d must contain one or two points")
    for index, point in enumerate(beak_points):
        _validate_point(point, f"beak_points_2d[{index}]")
    for field in (
        "beak_faces_camera",
        "holding_object",
        "use_local_depth_minimum",
    ):
        if not isinstance(value[field], bool):
            raise RuntimeError(f"{field} must be boolean")
    if not isinstance(value["notes"], str):
        raise RuntimeError("notes must be a string")
    return value


def validate_pose_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(
        _pose_validation_schema()["required"]
    ):
        raise RuntimeError("reviewed pose-validation fields do not match the required schema")
    if not isinstance(value["pose_reasonable"], bool):
        raise RuntimeError("pose_reasonable must be boolean")
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise RuntimeError("pose-validation confidence must be between 0 and 1")
    if value["box_fit"] not in {"GOOD", "ACCEPTABLE", "BAD"}:
        raise RuntimeError("box_fit is invalid")
    if value["orientation_fit"] not in {"GOOD", "ACCEPTABLE", "BAD"}:
        raise RuntimeError("orientation_fit is invalid")
    if not isinstance(value["matched_reference_view"], str):
        raise RuntimeError("matched_reference_view must be a string")
    reasons = value["reasons"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 4
        or any(not isinstance(item, str) for item in reasons)
    ):
        raise RuntimeError("reasons must contain at most four strings")
    return value


class ReviewedFileVision:
    """Bound exact local image evidence to an external multimodal review."""

    def __init__(
        self,
        workspace_root: Path,
        run_dir: Path,
        *,
        timeout_s: float,
    ):
        if timeout_s < 1.0 or timeout_s > 900.0:
            raise ValueError("review timeout must be between 1 and 900 seconds")
        self.workspace_root = workspace_root
        self.run_dir = run_dir
        self.timeout_s = float(timeout_s)
        self._localization_request_count = 0

    def _reference_artifact(self, label: str) -> dict[str, Any]:
        reference_root = (
            self.workspace_root
            / "providers"
            / "foundation_pose"
            / "defaults"
            / "rebot_b601_dm"
            / "references"
        )
        path = reference_root / f"{label.capitalize()}_reference_atlas.png"
        if not path.is_file():
            raise RuntimeError(f"{label} reference atlas is missing")
        payload = path.read_bytes()
        return {
            "label": label,
            "path": str(path.resolve()),
            "media_type": "image/png",
            "sha256": _sha256(payload),
            "bytes": len(payload),
        }

    async def _request(
        self,
        *,
        stem: str,
        review_kind: str,
        instructions: str,
        artifacts: list[dict[str, Any]],
        output_schema: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request_path = self.run_dir / f"{stem}_review_request.json"
        response_path = self.run_dir / f"{stem}_review_response.json"
        unsigned_request = {
            "schema": "midbrain.reviewed_multimodal_request",
            "schema_version": 1,
            "review_kind": review_kind,
            "created_at_us": time.time_ns() // 1000,
            "expires_at_us": int(
                (time.time() + self.timeout_s) * 1_000_000
            ),
            "instructions": instructions,
            "artifacts": artifacts,
            "output_schema": output_schema,
            "response_path": str(response_path.resolve()),
            "fallback_allowed": False,
        }
        request_sha256 = _canonical_sha256(unsigned_request)
        request = {**unsigned_request, "request_sha256": request_sha256}
        request_path.write_text(
            json.dumps(request, indent=2) + "\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + self.timeout_s
        while not response_path.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"reviewed multimodal response did not arrive within "
                    f"{self.timeout_s:.1f}s: {response_path}"
                )
            await asyncio.sleep(0.2)
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except Exception as error:
            raise RuntimeError(
                f"reviewed multimodal response is not valid JSON: {response_path}"
            ) from error
        if not isinstance(response, dict):
            raise RuntimeError("reviewed multimodal response must be an object")
        if response.get("schema") != "midbrain.reviewed_multimodal_response":
            raise RuntimeError("reviewed multimodal response schema is invalid")
        if response.get("schema_version") != 1:
            raise RuntimeError("reviewed multimodal response version is invalid")
        if response.get("review_kind") != review_kind:
            raise RuntimeError("reviewed multimodal response kind does not match")
        if response.get("request_sha256") != request_sha256:
            raise RuntimeError(
                "reviewed multimodal response does not bind the exact request"
            )
        reviewer = response.get("reviewer")
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("kind") not in {
                "CODEX_MULTIMODAL",
                "OPERATOR_REVIEWED_VLM",
            }
            or not str(reviewer.get("model") or "").strip()
        ):
            raise RuntimeError("reviewed multimodal reviewer provenance is incomplete")
        return response.get("result"), {
            "route": REVIEWED_FILE_ROUTE,
            "review_kind": review_kind,
            "request_path": str(request_path.resolve()),
            "response_path": str(response_path.resolve()),
            "request_sha256": request_sha256,
            "reviewer": reviewer,
            "fallback_allowed": False,
        }

    async def locate(
        self,
        rgb: np.ndarray,
        *,
        require_base: bool = True,
    ) -> dict[str, Any]:
        self._localization_request_count += 1
        request_stem = (
            "localization"
            if self._localization_request_count == 1
            else f"localization_vote_{self._localization_request_count}"
        )
        camera_path = self.run_dir / "camera.jpg"
        camera_payload = (
            camera_path.read_bytes()
            if camera_path.is_file()
            else encode_rgb_jpeg(rgb)
        )
        if not camera_path.is_file():
            camera_path.write_bytes(camera_payload)
        result, provenance = await self._request(
            stem=request_stem,
            review_kind="ROBOT_BASE_GRIPPER_LOCALIZATION",
            instructions=(
                "Review the exact live camera image and CAD atlases. Return tight "
                "visible-material [y0,x0,y1,x1] boxes, two safe interior [y,x] "
                "points for the stationary base and rigid gripper slider support, "
                "and one or two foremost physical beak points. All coordinates are "
                "integers normalized from 0 to 1000."
            ),
            artifacts=[
                {
                    "label": "live_camera",
                    "path": str(camera_path.resolve()),
                    "media_type": "image/jpeg",
                    "sha256": _sha256(camera_payload),
                    "bytes": len(camera_payload),
                },
                self._reference_artifact("base"),
                self._reference_artifact("gripper"),
            ],
            output_schema=_schema(),
        )
        validated = validate_localization_result(
            result,
            require_base=require_base,
        )
        return {**validated, "review_provenance": provenance}

    async def validate_base_pose(
        self,
        overlay_jpeg: bytes,
        *,
        attempt: int,
    ) -> dict[str, Any]:
        overlay_path = self.run_dir / f"foundation_pose_attempt_{attempt}_overlay.jpg"
        if not overlay_path.is_file():
            overlay_path.write_bytes(overlay_jpeg)
        result, provenance = await self._request(
            stem=f"pose_validation_attempt_{attempt}",
            review_kind="FOUNDATIONPOSE_BASE_VALIDATION",
            instructions=(
                "Compare the projected 3D base box and XYZ axes in the exact live "
                "overlay against the base CAD atlas. Judge object identity, scale, "
                "translation, perspective, and orientation. Reject a wrong object, "
                "gross offset, implausible scale, mirrored or upside-down pose."
            ),
            artifacts=[
                {
                    "label": "live_pose_overlay",
                    "path": str(overlay_path.resolve()),
                    "media_type": "image/jpeg",
                    "sha256": _sha256(overlay_jpeg),
                    "bytes": len(overlay_jpeg),
                },
                self._reference_artifact("base"),
            ],
            output_schema=_pose_validation_schema(),
        )
        validated = validate_pose_result(result)
        return {**validated, "review_provenance": provenance}

    async def close(self) -> None:
        return None


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
