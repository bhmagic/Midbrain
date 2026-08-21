from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from locate_arm_base.mask_candidates import (
    build_voted_mask,
    create_mask_candidate,
    write_multicolor_mask_overlay,
)


def _candidate(
    tmp_path: Path,
    rgb_path: Path,
    candidate_id: str,
    mask: np.ndarray,
):
    mask_path = tmp_path / f"{candidate_id}.png"
    Image.fromarray(mask.astype(np.uint8) * 255).save(mask_path)
    return create_mask_candidate(
        candidate_id=candidate_id,
        mask_path=mask_path,
        rgb_path=rgb_path,
        output_dir=tmp_path / "overlays",
        prompt={"positive_points_yx": [[500, 500]]},
        sam2_score=0.9,
        sam2_provenance={"provider_id": "test.sam2"},
    )


def test_voted_mask_uses_all_acquired_candidates_at_half_and_dilates_once(
    tmp_path: Path,
) -> None:
    rgb_path = tmp_path / "rgb.png"
    Image.new("RGB", (40, 40), "#404040").save(rgb_path)
    first = np.zeros((40, 40), dtype=bool)
    second = np.zeros((40, 40), dtype=bool)
    third = np.zeros((40, 40), dtype=bool)
    bad = np.zeros((40, 40), dtype=bool)
    first[10:20, 10:20] = True
    second[11:21, 10:20] = True
    third[10:20, 11:21] = True
    bad[25:35, 25:35] = True
    candidates = tuple(
        _candidate(tmp_path, rgb_path, candidate_id, mask)
        for candidate_id, mask in (
            ("mask_1", first),
            ("mask_2", second),
            ("mask_3", third),
            ("mask_4", bad),
        )
    )

    voted = build_voted_mask(
        candidates=candidates,
        retained_candidate_ids=("mask_1", "mask_2", "mask_3", "mask_4"),
        rgb_path=rgb_path,
        output_dir=tmp_path / "vote",
        dilation_radius_px=2,
    )

    expected = (
        first.astype(int)
        + second.astype(int)
        + third.astype(int)
        + bad.astype(int)
    ) >= 2
    actual = np.asarray(Image.open(voted.voted_mask_path).convert("L")) > 0
    assert np.array_equal(actual, expected)
    assert voted.vote_threshold == 2
    assert voted.vote_policy == "AT_LEAST_HALF_OF_ALL_ACQUIRED_MASKS"
    assert voted.retained_candidate_ids == (
        "mask_1",
        "mask_2",
        "mask_3",
        "mask_4",
    )
    assert voted.final_nonzero_pixels > voted.voted_nonzero_pixels
    assert voted.voted_overlay_path.is_file()
    assert voted.final_overlay_path.is_file()


def test_four_surviving_masks_require_two_pixel_votes(tmp_path: Path) -> None:
    rgb_path = tmp_path / "rgb.png"
    Image.new("RGB", (20, 20), "#202020").save(rgb_path)
    masks = []
    for index in range(4):
        mask = np.zeros((20, 20), dtype=bool)
        mask[5:10, 5 + (index % 2) : 10 + (index % 2)] = True
        masks.append(_candidate(tmp_path, rgb_path, f"mask_{index + 1}", mask))

    voted = build_voted_mask(
        candidates=tuple(masks),
        retained_candidate_ids=tuple(candidate.candidate_id for candidate in masks),
        rgb_path=rgb_path,
        output_dir=tmp_path / "vote",
        dilation_radius_px=0,
    )

    assert voted.survivor_count == 4
    assert voted.vote_threshold == 2
    assert voted.final_nonzero_pixels == voted.voted_nonzero_pixels


def test_multicolor_overlay_uses_one_shared_rgb_frame(tmp_path: Path) -> None:
    rgb_path = tmp_path / "rgb.png"
    Image.new("RGB", (200, 180), "#303030").save(rgb_path)
    first = np.zeros((180, 200), dtype=bool)
    second = np.zeros((180, 200), dtype=bool)
    first[110:150, 20:70] = True
    second[110:150, 130:180] = True
    candidates = (
        _candidate(tmp_path, rgb_path, "mask_1", first),
        _candidate(tmp_path, rgb_path, "mask_2", second),
    )

    output = write_multicolor_mask_overlay(
        rgb_path=rgb_path,
        candidates=candidates,
        output_path=tmp_path / "multicolor.png",
    )

    rendered = np.asarray(Image.open(output).convert("RGB"))
    assert rendered.shape == (180, 200, 3)
    assert not np.array_equal(rendered[125, 35], rendered[125, 145])
