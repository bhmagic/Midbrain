from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .prompts import VisualPrompt


def _normalized_yx(value: tuple[int, int], shape: tuple[int, int]) -> tuple[float, float]:
    height, width = shape
    y = float(value[0]) * max(0, height - 1) / 1000.0
    x = float(value[1]) * max(0, width - 1) / 1000.0
    return y, x


def _prompt_arrays(
    prompt: VisualPrompt,
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y0, x0 = _normalized_yx(prompt.box_yxyx[:2], shape)
    y1, x1 = _normalized_yx(prompt.box_yxyx[2:], shape)
    normalized_points = prompt.positive_points_yx + prompt.negative_points_yx
    points = np.asarray(
        [
            [x, y]
            for y, x in (
                _normalized_yx(value, shape)
                for value in normalized_points
            )
        ],
        dtype=np.float32,
    )
    labels = np.asarray(
        [1] * len(prompt.positive_points_yx)
        + [0] * len(prompt.negative_points_yx),
        dtype=np.int32,
    )
    return points, labels, np.asarray([x0, y0, x1, y1], dtype=np.float32)


def prompt_from_mask(
    object_id: str,
    mask: np.ndarray,
    *,
    expansion_fraction: float = 0.10,
) -> VisualPrompt:
    binary = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    ys, xs = np.nonzero(binary)
    if ys.size == 0:
        raise ValueError("cannot build a SAM2 tracking prompt from an empty mask")
    height, width = binary.shape
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    pad_y = int(round((y1 - y0) * float(expansion_fraction) / 2.0))
    pad_x = int(round((x1 - x0) * float(expansion_fraction) / 2.0))
    y0, y1 = max(0, y0 - pad_y), min(height, y1 + pad_y)
    x0, x1 = max(0, x0 - pad_x), min(width, x1 + pad_x)
    distance = cv2.distanceTransform(binary.astype(np.uint8), cv2.DIST_L2, 5)
    first_yx = np.unravel_index(int(np.argmax(distance)), distance.shape)
    candidate_score = distance.copy()
    yy, xx = np.indices(binary.shape)
    separation = np.sqrt((yy - first_yx[0]) ** 2 + (xx - first_yx[1]) ** 2)
    candidate_score *= separation
    second_yx = np.unravel_index(int(np.argmax(candidate_score)), distance.shape)

    def normalize(y: int, x: int) -> tuple[int, int]:
        return (
            int(round(y * 1000.0 / max(1, height - 1))),
            int(round(x * 1000.0 / max(1, width - 1))),
        )

    return VisualPrompt(
        object_id=object_id,
        region_id="sam2-short-range-track",
        box_yxyx=(
            *normalize(y0, x0),
            *normalize(max(y0 + 1, y1 - 1), max(x0 + 1, x1 - 1)),
        ),
        positive_points_yx=(
            normalize(int(first_yx[0]), int(first_yx[1])),
            normalize(int(second_yx[0]), int(second_yx[1])),
        ),
        confidence=0.5,
    )


class Sam2ImageTracker:
    """One loaded SAM2 predictor used for label refresh and short-range tracking."""

    CONFIG = "configs/sam2.1/sam2.1_hiera_b+.yaml"

    def __init__(self, checkpoint: Path) -> None:
        self.checkpoint = Path(checkpoint).resolve()
        self.predictor: Any = None
        self.torch: Any = None
        self.current_shape: tuple[int, int] | None = None

    def load(self) -> None:
        if self.predictor is not None:
            return
        if not self.checkpoint.is_file():
            raise FileNotFoundError(f"SAM2 checkpoint is missing: {self.checkpoint}")
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_sam2(self.CONFIG, str(self.checkpoint), device=device)
        self.predictor = SAM2ImagePredictor(model)
        self.torch = torch

    def set_image(self, image_rgb: np.ndarray) -> None:
        self.load()
        image = np.ascontiguousarray(np.asarray(image_rgb, dtype=np.uint8))
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("SAM2 image must have shape HxWx3")
        assert self.predictor is not None
        self.predictor.set_image(image)
        self.current_shape = image.shape[:2]

    def segment(self, prompts: list[VisualPrompt]) -> tuple[np.ndarray, float]:
        if self.predictor is None or self.current_shape is None:
            raise RuntimeError("set_image must be called before SAM2 segmentation")
        combined = np.zeros(self.current_shape, dtype=bool)
        scores: list[float] = []
        for prompt in prompts:
            points, point_labels, box = _prompt_arrays(prompt, self.current_shape)
            masks, raw_scores, _ = self.predictor.predict(
                point_coords=points,
                point_labels=point_labels,
                box=box,
                multimask_output=True,
            )
            candidates = np.asarray(masks, dtype=bool)
            if candidates.ndim == 2:
                candidates = candidates[None, ...]
            score_values = np.asarray(raw_scores, dtype=float).reshape(-1)
            ranked: list[tuple[int, float, int]] = []
            for index, candidate in enumerate(candidates):
                positive_count = len(prompt.positive_points_yx)
                inside_positive = sum(
                    int(candidate[int(round(y)), int(round(x))])
                    for x, y in points[:positive_count]
                )
                inside_negative = sum(
                    int(candidate[int(round(y)), int(round(x))])
                    for x, y in points[positive_count:]
                )
                area_fraction = float(candidate.mean())
                plausible = 0.0001 <= area_fraction <= 0.95
                prompt_agreement = inside_positive - inside_negative
                ranked.append(
                    (prompt_agreement + int(plausible), score_values[index], index)
                )
            _, score, selected = max(ranked)
            combined |= candidates[selected]
            scores.append(float(score))
        if not np.any(combined):
            raise RuntimeError("SAM2 returned an empty combined mask")
        return np.ascontiguousarray(combined), min(scores)

    def close(self) -> None:
        self.predictor = None
        torch = self.torch
        self.torch = None
        self.current_shape = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
