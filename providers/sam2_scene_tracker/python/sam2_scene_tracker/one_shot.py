from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from .prompts import VisualPrompt, parse_visual_prompts


class PromptedImageSegmenter(Protocol):
    def set_image(self, image_rgb: np.ndarray) -> None: ...

    def segment(
        self,
        prompts: list[VisualPrompt],
    ) -> tuple[np.ndarray, float]: ...


def segment_workspace_image(
    *,
    payload: Any,
    tracker: PromptedImageSegmenter,
    tracker_lock: threading.Lock,
    workspace_root: Path,
    artifact_root: Path,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
) -> dict[str, Any]:
    """Segment one workspace image from a caller-supplied visual prompt."""
    if not isinstance(payload, dict):
        raise ValueError("segment_image payload must be an object")
    request_id = str(payload.get("request_id") or uuid.uuid4()).strip()
    image_path = Path(str(payload.get("rgb_path") or "")).resolve()
    try:
        image_path.relative_to(workspace_root.resolve())
    except ValueError as error:
        raise ValueError(
            "segment_image rgb_path must be inside the workspace"
        ) from error
    if not image_path.is_file():
        raise ValueError("segment_image rgb_path is unavailable")

    image_bytes = image_path.read_bytes()
    actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
    expected_sha256 = str(payload.get("rgb_sha256") or "").strip().lower()
    if expected_sha256 and expected_sha256 != actual_sha256:
        raise ValueError("segment_image RGB SHA-256 does not match")

    prompts = parse_visual_prompts(
        {
            "detections": [
                {
                    "object_id": "arm_base",
                    "region_id": "locate-arm-base-seed",
                    "box_2d": payload.get("box_yxyx"),
                    "positive_points_2d": payload.get("positive_points_yx"),
                    "negative_points_2d": payload.get("negative_points_yx"),
                    "confidence": payload.get("prompt_confidence"),
                }
            ]
        },
        expected_object_ids={"arm_base"},
    )["arm_base"]
    image_rgb = np.asarray(Image.open(image_path).convert("RGB"), dtype=np.uint8)
    with tracker_lock:
        tracker.set_image(image_rgb)
        mask, score = tracker.segment(prompts)

    binary_mask = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    if binary_mask.shape != image_rgb.shape[:2]:
        raise RuntimeError("SAM2 mask shape does not match the source RGB image")
    if not np.any(binary_mask):
        raise RuntimeError("SAM2 returned an empty arm-base mask")

    artifact_root.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(
        character
        for character in request_id
        if character.isalnum() or character in "-_"
    )[:96]
    if not safe_id:
        safe_id = str(uuid.uuid4())
    mask_path = artifact_root / f"{safe_id}.png"
    Image.fromarray(binary_mask.astype(np.uint8) * 255).save(
        mask_path,
        format="PNG",
    )
    mask_sha256 = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    return {
        "status": "SEGMENTED",
        "request_id": request_id,
        "mask_artifact": {
            "path": str(mask_path),
            "sha256": mask_sha256,
            "width": int(binary_mask.shape[1]),
            "height": int(binary_mask.shape[0]),
            "nonzero_pixels": int(np.count_nonzero(binary_mask)),
        },
        "quality": {"sam2_score": float(score)},
        "provenance": {
            "provider_id": provider_id,
            "provider_instance_id": provider_instance_id,
            "boot_id": boot_id,
            "rgb_sha256": actual_sha256,
            "prompt_contract": (
                "VLM_BOX_PLUS_POSITIVE_AND_OPTIONAL_NEGATIVE_POINTS_NORMALIZED_1000"
            ),
        },
    }
