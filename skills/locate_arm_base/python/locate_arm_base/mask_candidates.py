from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


MASK_OVERLAY_COLORS = (
    (255, 76, 76),
    (76, 220, 255),
    (110, 235, 120),
    (255, 195, 70),
    (190, 120, 255),
    (255, 105, 190),
    (80, 145, 255),
    (220, 235, 80),
)


@dataclass(frozen=True)
class MaskCandidate:
    candidate_id: str
    mask_path: Path
    overlay_path: Path
    nonzero_pixels: int
    prompt: dict[str, Any]
    sam2_score: float
    sam2_provenance: dict[str, Any] | None

    def record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "mask_path": str(self.mask_path),
            "overlay_path": str(self.overlay_path),
            "nonzero_pixels": self.nonzero_pixels,
            "prompt": self.prompt,
            "sam2_score": self.sam2_score,
            "sam2_provenance": self.sam2_provenance,
        }


@dataclass(frozen=True)
class VotedMask:
    mask_id: str
    retained_candidate_ids: tuple[str, ...]
    survivor_count: int
    vote_threshold: int
    vote_policy: str
    voted_mask_path: Path
    voted_overlay_path: Path
    voted_nonzero_pixels: int
    dilation_radius_px: int
    final_mask_path: Path
    final_overlay_path: Path
    final_nonzero_pixels: int

    def record(self) -> dict[str, Any]:
        return {
            "mask_id": self.mask_id,
            "retained_candidate_ids": list(self.retained_candidate_ids),
            "survivor_count": self.survivor_count,
            "vote_threshold": self.vote_threshold,
            "vote_policy": self.vote_policy,
            "voted_mask_path": str(self.voted_mask_path),
            "voted_overlay_path": str(self.voted_overlay_path),
            "voted_nonzero_pixels": self.voted_nonzero_pixels,
            "dilation_radius_px": self.dilation_radius_px,
            "final_mask_path": str(self.final_mask_path),
            "final_overlay_path": str(self.final_overlay_path),
            "final_nonzero_pixels": self.final_nonzero_pixels,
        }


def _read_mask(mask_path: Path, expected_size: tuple[int, int]) -> np.ndarray:
    mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8) > 0
    expected_width, expected_height = expected_size
    if mask.shape != (expected_height, expected_width):
        raise ValueError("mask candidate dimensions do not match RGB evidence")
    if not np.any(mask):
        raise ValueError("mask candidate is empty")
    return np.ascontiguousarray(mask)


def _dilate(mask: np.ndarray, radius_px: int) -> np.ndarray:
    if radius_px <= 0:
        return np.ascontiguousarray(mask, dtype=bool)
    size = radius_px * 2 + 1
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.asarray(image.filter(ImageFilter.MaxFilter(size=size))) > 0


def write_mask_overlay(
    rgb_path: Path,
    mask_path: Path,
    output_path: Path,
    *,
    label: str,
    footer: str,
) -> Path:
    image = Image.open(rgb_path).convert("RGBA")
    mask = _read_mask(mask_path, image.size)
    overlay = np.zeros((image.height, image.width, 4), dtype=np.uint8)
    overlay[mask] = (255, 52, 52, 112)
    composite = Image.alpha_composite(image, Image.fromarray(overlay))
    edge = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.FIND_EDGES)
    outline = np.zeros_like(overlay)
    outline[np.asarray(edge, dtype=np.uint8) > 0] = (255, 230, 80, 255)
    composite = Image.alpha_composite(composite, Image.fromarray(outline))
    draw = ImageDraw.Draw(composite)
    header = f"{label}  pixels={int(mask.sum())}"
    draw.rectangle((0, 0, min(image.width, 900), 34), fill=(0, 0, 0, 220))
    draw.text((10, 9), header, fill="white", font=ImageFont.load_default())
    draw.text(
        (10, image.height - 22),
        footer,
        fill=(255, 240, 160, 255),
        font=ImageFont.load_default(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.convert("RGB").save(output_path, format="PNG")
    return output_path


def write_multicolor_mask_overlay(
    *,
    rgb_path: Path,
    candidates: tuple[MaskCandidate, ...],
    output_path: Path,
) -> Path:
    """Render all independent masks over one unduplicated RGB frame."""

    if not candidates:
        raise ValueError("multicolor mask overlay requires at least one candidate")
    image = Image.open(rgb_path).convert("RGBA")
    composite = image
    masks: list[tuple[MaskCandidate, np.ndarray, tuple[int, int, int]]] = []
    for index, candidate in enumerate(candidates):
        color = MASK_OVERLAY_COLORS[index % len(MASK_OVERLAY_COLORS)]
        mask = _read_mask(candidate.mask_path, image.size)
        masks.append((candidate, mask, color))
        tint = np.zeros((image.height, image.width, 4), dtype=np.uint8)
        tint[mask] = (*color, 76)
        composite = Image.alpha_composite(composite, Image.fromarray(tint))

    for _, mask, color in masks:
        edge = Image.fromarray(mask.astype(np.uint8) * 255).filter(
            ImageFilter.FIND_EDGES
        )
        outline = np.zeros((image.height, image.width, 4), dtype=np.uint8)
        outline[np.asarray(edge, dtype=np.uint8) > 0] = (*color, 255)
        composite = Image.alpha_composite(composite, Image.fromarray(outline))

    draw = ImageDraw.Draw(composite)
    legend_height = 26 + len(masks) * 20
    draw.rectangle(
        (0, 0, min(image.width, 720), legend_height),
        fill=(0, 0, 0, 215),
    )
    draw.text(
        (10, 8),
        "Independent SAM2 masks (one shared RGB frame)",
        fill="white",
        font=ImageFont.load_default(),
    )
    for index, (candidate, mask, color) in enumerate(masks):
        y = 28 + index * 20
        draw.rectangle((10, y, 24, y + 12), fill=(*color, 255))
        draw.text(
            (32, y),
            f"{candidate.candidate_id}  pixels={int(mask.sum())}",
            fill=(*color, 255),
            font=ImageFont.load_default(),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.convert("RGB").save(output_path, format="PNG")
    return output_path


def create_mask_candidate(
    *,
    candidate_id: str,
    mask_path: Path,
    rgb_path: Path,
    output_dir: Path,
    prompt: dict[str, Any],
    sam2_score: float,
    sam2_provenance: dict[str, Any] | None,
) -> MaskCandidate:
    image = Image.open(rgb_path)
    mask = _read_mask(mask_path, image.size)
    overlay_path = output_dir / f"{candidate_id}_overlay.png"
    write_mask_overlay(
        rgb_path,
        mask_path,
        overlay_path,
        label=candidate_id,
        footer="INDEPENDENT_VLM_POINT_TO_SAM2_MASK",
    )
    return MaskCandidate(
        candidate_id=candidate_id,
        mask_path=mask_path,
        overlay_path=overlay_path,
        nonzero_pixels=int(mask.sum()),
        prompt=prompt,
        sam2_score=float(sam2_score),
        sam2_provenance=sam2_provenance,
    )


def build_voted_mask(
    *,
    candidates: tuple[MaskCandidate, ...],
    retained_candidate_ids: tuple[str, ...],
    rgb_path: Path,
    output_dir: Path,
    dilation_radius_px: int,
) -> VotedMask:
    if not candidates:
        raise ValueError("mask voting requires at least one candidate")
    if not 0 <= dilation_radius_px <= 64:
        raise ValueError("final mask dilation radius must be between 0 and 64 pixels")
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if not retained_candidate_ids or len(set(retained_candidate_ids)) != len(
        retained_candidate_ids
    ):
        raise ValueError("mask voting requires a non-empty unique retained set")
    if any(candidate_id not in candidate_by_id for candidate_id in retained_candidate_ids):
        raise ValueError("mask voting retained an unknown candidate")
    if len(retained_candidate_ids) != len(candidates):
        raise ValueError("mask voting must retain every successfully acquired candidate")
    image = Image.open(rgb_path)
    retained_masks = [
        _read_mask(candidate_by_id[candidate_id].mask_path, image.size)
        for candidate_id in retained_candidate_ids
    ]
    survivor_count = len(retained_masks)
    vote_threshold = int(math.ceil(survivor_count / 2.0))
    vote_counts = np.sum(np.stack(retained_masks, axis=0), axis=0)
    voted = vote_counts >= vote_threshold
    if not np.any(voted):
        raise RuntimeError("retained masks produced an empty pixel vote")
    output_dir.mkdir(parents=True, exist_ok=True)
    voted_mask_path = output_dir / "voted_mask.png"
    Image.fromarray(voted.astype(np.uint8) * 255).save(voted_mask_path, format="PNG")
    voted_overlay_path = output_dir / "voted_mask_overlay.png"
    write_mask_overlay(
        rgb_path,
        voted_mask_path,
        voted_overlay_path,
        label="voted_mask",
        footer=f"PIXEL_VOTE_AT_LEAST_{vote_threshold}_OF_{survivor_count}",
    )
    final_mask = _dilate(voted, dilation_radius_px)
    final_mask_path = output_dir / f"voted_mask_dilated_r{dilation_radius_px}.png"
    Image.fromarray(final_mask.astype(np.uint8) * 255).save(
        final_mask_path, format="PNG"
    )
    final_overlay_path = output_dir / f"voted_mask_dilated_r{dilation_radius_px}_overlay.png"
    write_mask_overlay(
        rgb_path,
        final_mask_path,
        final_overlay_path,
        label=f"voted_mask_dilated_r{dilation_radius_px}",
        footer="SINGLE_POST_VOTE_DILATION",
    )
    return VotedMask(
        mask_id=f"voted_mask_dilated_r{dilation_radius_px}",
        retained_candidate_ids=retained_candidate_ids,
        survivor_count=survivor_count,
        vote_threshold=vote_threshold,
        vote_policy="AT_LEAST_HALF_OF_ALL_ACQUIRED_MASKS",
        voted_mask_path=voted_mask_path,
        voted_overlay_path=voted_overlay_path,
        voted_nonzero_pixels=int(voted.sum()),
        dilation_radius_px=dilation_radius_px,
        final_mask_path=final_mask_path,
        final_overlay_path=final_overlay_path,
        final_nonzero_pixels=int(final_mask.sum()),
    )


def build_image_contact_sheet(
    image_paths: tuple[Path, ...], output_path: Path
) -> Path:
    if not image_paths:
        raise ValueError("contact sheet requires at least one image")
    panels: list[Image.Image] = []
    for path in image_paths:
        panel = Image.open(path).convert("RGB")
        panel.thumbnail((960, 540), Image.Resampling.LANCZOS)
        framed = Image.new("RGB", (980, 560), "#080808")
        framed.paste(panel, ((980 - panel.width) // 2, (560 - panel.height) // 2))
        panels.append(framed)
    columns = 2 if len(panels) > 1 else 1
    rows = (len(panels) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 980, rows * 560), "#030303")
    for index, panel in enumerate(panels):
        sheet.paste(panel, ((index % columns) * 980, (index // columns) * 560))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path
