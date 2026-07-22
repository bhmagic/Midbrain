"""Point-and-box prompted SAM2 segmentation for GUI initialization."""

from __future__ import annotations

import gc
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .bounding_box import BoundingBoxMask
from .vlm_detection import NormalizedPoint


@dataclass(frozen=True)
class CropBounds:
    y0: int
    x0: int
    y1: int
    x1: int

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def width(self) -> int:
        return self.x1 - self.x0


@dataclass(frozen=True)
class MaskResult:
    mask: np.ndarray
    predicted_iou: float
    crop: CropBounds
    sam_pixel_count: int
    sam_mask: np.ndarray
    median_rgb: tuple[int, int, int] | None = None
    median_lab: tuple[int, int, int] | None = None
    refinement_method: str = "none"


def box_pixel_bounds(box: BoundingBoxMask, height: int, width: int) -> CropBounds:
    """Convert a supported box to clipped half-open pixel bounds."""
    mask = box.to_mask(height, width)
    ys, xs = np.where(mask)
    if not len(xs):
        raise ValueError("bounding box does not overlap the image")
    return CropBounds(int(ys.min()), int(xs.min()), int(ys.max()) + 1, int(xs.max()) + 1)


def padded_crop_bounds(
    box: BoundingBoxMask,
    height: int,
    width: int,
    *,
    expansion_fraction: float = 0.5,
) -> CropBounds:
    """Expand total box dimensions by a fraction, split equally on every side."""
    if not math.isfinite(expansion_fraction) or not 0.0 <= expansion_fraction <= 2.0:
        raise ValueError("crop expansion must be between 0 and 2")
    bounds = box_pixel_bounds(box, height, width)
    pad_y = bounds.height * expansion_fraction / 2.0
    pad_x = bounds.width * expansion_fraction / 2.0
    return CropBounds(
        max(0, int(math.floor(bounds.y0 - pad_y))),
        max(0, int(math.floor(bounds.x0 - pad_x))),
        min(height, int(math.ceil(bounds.y1 + pad_y))),
        min(width, int(math.ceil(bounds.x1 + pad_x))),
    )


def _point_pixels(
    points: tuple[NormalizedPoint, NormalizedPoint],
    height: int,
    width: int,
) -> np.ndarray:
    xy = []
    for point in points:
        y, x = point.to_pixel_yx(height, width)
        xy.append([min(width - 1, max(0.0, x)), min(height - 1, max(0.0, y))])
    return np.asarray(xy, dtype=np.float32)


def _keep_prompt_components(mask: np.ndarray, point_xy: np.ndarray) -> np.ndarray:
    binary = np.ascontiguousarray(mask.astype(np.uint8))
    count, labels = cv2.connectedComponents(binary, connectivity=8)
    if count <= 1:
        return binary.astype(bool)
    selected: set[int] = set()
    height, width = binary.shape
    for x, y in point_xy:
        label = int(labels[min(height - 1, max(0, round(float(y)))), min(width - 1, max(0, round(float(x))))])
        if label:
            selected.add(label)
    if not selected:
        return binary.astype(bool)
    return np.isin(labels, list(selected))


def choose_mask(
    masks: np.ndarray,
    scores: np.ndarray,
    point_xy: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Choose a plausible candidate containing both positive prompt points."""
    candidates = np.asarray(masks, dtype=bool)
    if candidates.ndim == 2:
        candidates = candidates[None, ...]
    score_values = np.asarray(scores, dtype=float).reshape(-1)
    if len(candidates) != len(score_values):
        raise ValueError("SAM2 returned mismatched masks and scores")
    height, width = candidates.shape[1:]
    ranked: list[tuple[int, float, int]] = []
    for index, candidate in enumerate(candidates):
        inside = 0
        for x, y in point_xy:
            px = min(width - 1, max(0, round(float(x))))
            py = min(height - 1, max(0, round(float(y))))
            inside += int(candidate[py, px])
        area_fraction = float(candidate.mean())
        plausible = 0.001 <= area_fraction <= 0.75
        ranked.append((inside + int(plausible), float(score_values[index]), index))
    _, score, best_index = max(ranked)
    cleaned = _keep_prompt_components(candidates[best_index], point_xy)
    if not np.any(cleaned):
        raise RuntimeError("SAM2 returned an empty mask")
    return np.ascontiguousarray(cleaned), score


def refine_mask_by_median_rgb(
    image_rgb: np.ndarray,
    seed_mask: np.ndarray,
    crop: CropBounds,
    *,
    tolerance_fraction: float = 0.10,
    dilation_radius: int = 2,
) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Grow seed-connected pixels near the median RGB, then dilate inside crop."""
    if image_rgb.shape[:2] != seed_mask.shape:
        raise ValueError("image and seed mask dimensions must match")
    if not 0.0 <= tolerance_fraction <= 1.0:
        raise ValueError("RGB tolerance fraction must be between 0 and 1")
    if dilation_radius < 0:
        raise ValueError("dilation radius cannot be negative")
    crop_image = image_rgb[crop.y0 : crop.y1, crop.x0 : crop.x1]
    crop_seed = np.asarray(
        seed_mask[crop.y0 : crop.y1, crop.x0 : crop.x1], dtype=bool
    )
    if not np.any(crop_seed):
        raise ValueError("color refinement seed mask is empty inside crop")

    median = np.median(crop_image[crop_seed], axis=0)
    tolerance = 255.0 * tolerance_fraction
    color_candidate = np.all(
        np.abs(crop_image.astype(np.float32) - median.reshape(1, 1, 3))
        <= tolerance,
        axis=2,
    )
    count, labels = cv2.connectedComponents(
        np.ascontiguousarray(color_candidate.astype(np.uint8)), connectivity=8
    )
    touching = set(int(value) for value in np.unique(labels[crop_seed]) if value)
    grown = crop_seed.copy()
    if count > 1 and touching:
        grown |= np.isin(labels, list(touching))
    if dilation_radius:
        size = dilation_radius * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        grown = cv2.dilate(grown.astype(np.uint8), kernel, iterations=1) > 0

    result = np.zeros(seed_mask.shape, dtype=bool)
    result[crop.y0 : crop.y1, crop.x0 : crop.x1] = grown
    median_rgb = tuple(int(round(value)) for value in median)
    return np.ascontiguousarray(result), median_rgb  # type: ignore[return-value]


def refine_mask_by_median_lab(
    image_rgb: np.ndarray,
    seed_mask: np.ndarray,
    crop: CropBounds,
    *,
    distance_threshold: float = 30.0,
    dilation_radius: int = 2,
) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Grow seed-connected pixels within a median Lab distance, then dilate."""
    if image_rgb.shape[:2] != seed_mask.shape:
        raise ValueError("image and seed mask dimensions must match")
    if not math.isfinite(distance_threshold) or distance_threshold < 0.0:
        raise ValueError("Lab distance threshold must be finite and non-negative")
    if dilation_radius < 0:
        raise ValueError("dilation radius cannot be negative")

    crop_image = np.ascontiguousarray(
        image_rgb[crop.y0 : crop.y1, crop.x0 : crop.x1]
    )
    crop_seed = np.asarray(
        seed_mask[crop.y0 : crop.y1, crop.x0 : crop.x1], dtype=bool
    )
    if not np.any(crop_seed):
        raise ValueError("color refinement seed mask is empty inside crop")

    crop_lab = cv2.cvtColor(crop_image, cv2.COLOR_RGB2LAB)
    median = np.median(crop_lab[crop_seed], axis=0)
    difference = crop_lab.astype(np.float32) - median.reshape(1, 1, 3)
    color_candidate = np.linalg.norm(difference, axis=2) <= distance_threshold
    count, labels = cv2.connectedComponents(
        np.ascontiguousarray(color_candidate.astype(np.uint8)), connectivity=8
    )
    touching = set(int(value) for value in np.unique(labels[crop_seed]) if value)
    grown = crop_seed.copy()
    if count > 1 and touching:
        grown |= np.isin(labels, list(touching))
    if dilation_radius:
        size = dilation_radius * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        grown = cv2.dilate(grown.astype(np.uint8), kernel, iterations=1) > 0

    result = np.zeros(seed_mask.shape, dtype=bool)
    result[crop.y0 : crop.y1, crop.x0 : crop.x1] = grown
    median_lab = tuple(int(round(value)) for value in median)
    return np.ascontiguousarray(result), median_lab  # type: ignore[return-value]


class Sam2Segmenter:
    """Lazy owner of one SAM2 image predictor."""

    CONFIG = "configs/sam2.1/sam2.1_hiera_b+.yaml"

    def __init__(self, provider_root: Path, *, checkpoint: Path | None = None) -> None:
        self.provider_root = provider_root.resolve()
        self.checkpoint = (
            checkpoint
            or self.provider_root / "sam2" / "checkpoints" / "sam2.1_hiera_base_plus.pt"
        ).resolve()
        self._predictor: Any = None
        self._torch: Any = None

    def _load(self) -> None:
        if self._predictor is not None:
            return
        if not self.checkpoint.is_file():
            raise FileNotFoundError(
                f"SAM2.1 Base+ checkpoint is missing: {self.checkpoint}. "
                "Run scripts/setup_sam2.ps1 first."
            )
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_sam2(self.CONFIG, str(self.checkpoint), device=device)
        self._predictor = SAM2ImagePredictor(model)
        self._torch = torch

    def segment(
        self,
        image_rgb: np.ndarray,
        box: BoundingBoxMask,
        points: tuple[NormalizedPoint, NormalizedPoint],
        *,
        crop_expansion: float = 0.5,
        color_refine: bool = False,
        color_refine_space: str = "rgb",
        color_tolerance_fraction: float = 0.10,
        lab_distance_threshold: float = 30.0,
        dilation_radius: int = 2,
    ) -> MaskResult:
        self._load()
        height, width = image_rgb.shape[:2]
        crop = padded_crop_bounds(
            box, height, width, expansion_fraction=crop_expansion
        )
        box_bounds = box_pixel_bounds(box, height, width)
        crop_image = np.ascontiguousarray(
            image_rgb[crop.y0 : crop.y1, crop.x0 : crop.x1]
        )
        point_xy_full = _point_pixels(points, height, width)
        point_xy_crop = point_xy_full - np.asarray([crop.x0, crop.y0], dtype=np.float32)
        box_xyxy = np.asarray(
            [
                box_bounds.x0 - crop.x0,
                box_bounds.y0 - crop.y0,
                box_bounds.x1 - 1 - crop.x0,
                box_bounds.y1 - 1 - crop.y0,
            ],
            dtype=np.float32,
        )
        assert self._predictor is not None
        self._predictor.set_image(crop_image)
        masks, scores, _ = self._predictor.predict(
            point_coords=point_xy_crop,
            point_labels=np.ones(2, dtype=np.int32),
            box=box_xyxy,
            multimask_output=True,
        )
        crop_mask, score = choose_mask(masks, scores, point_xy_crop)
        full_mask = np.zeros((height, width), dtype=bool)
        full_mask[crop.y0 : crop.y1, crop.x0 : crop.x1] = crop_mask
        sam_pixel_count = int(full_mask.sum())
        sam_mask = np.ascontiguousarray(full_mask.copy())
        median_rgb = None
        median_lab = None
        refinement_method = "none"
        if color_refine:
            if color_refine_space == "rgb":
                full_mask, median_rgb = refine_mask_by_median_rgb(
                    image_rgb,
                    full_mask,
                    crop,
                    tolerance_fraction=color_tolerance_fraction,
                    dilation_radius=dilation_radius,
                )
                refinement_method = f"median_rgb_{color_tolerance_fraction:.3f}_r{dilation_radius}"
            elif color_refine_space == "lab":
                full_mask, median_lab = refine_mask_by_median_lab(
                    image_rgb,
                    full_mask,
                    crop,
                    distance_threshold=lab_distance_threshold,
                    dilation_radius=dilation_radius,
                )
                refinement_method = f"median_lab_d{lab_distance_threshold:g}_r{dilation_radius}"
            else:
                raise ValueError("color refinement space must be 'rgb' or 'lab'")
        return MaskResult(
            np.ascontiguousarray(full_mask),
            score,
            crop,
            sam_pixel_count,
            sam_mask,
            median_rgb,
            median_lab,
            refinement_method,
        )

    def close(self) -> None:
        self._predictor = None
        torch = self._torch
        self._torch = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def __enter__(self) -> "Sam2Segmenter":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
