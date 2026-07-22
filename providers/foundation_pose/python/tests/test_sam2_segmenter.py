from __future__ import annotations

import cv2
import numpy as np

from foundation_pose_provider.bounding_box import BoundingBoxMask
from foundation_pose_provider.sam2_segmenter import (
    CropBounds,
    choose_mask,
    padded_crop_bounds,
    refine_mask_by_median_lab,
    refine_mask_by_median_rgb,
)


def test_crop_expands_total_dimensions_by_fifty_percent() -> None:
    box = BoundingBoxMask((200, 200, 600, 600))
    assert padded_crop_bounds(box, 100, 200, expansion_fraction=0.5) == CropBounds(
        10, 20, 70, 140
    )


def test_crop_clips_at_image_boundary() -> None:
    box = BoundingBoxMask((0, 0, 200, 200))
    assert padded_crop_bounds(box, 100, 100, expansion_fraction=0.5) == CropBounds(
        0, 0, 25, 25
    )


def test_choose_mask_prefers_candidate_containing_both_points() -> None:
    masks = np.zeros((3, 20, 20), dtype=bool)
    masks[0, 1:19, 1:19] = True
    masks[1, 4:16, 4:16] = True
    masks[2, 8:12, 8:12] = True
    points = np.asarray([[9, 9], [14, 14]], dtype=np.float32)
    selected, score = choose_mask(masks, np.asarray([0.99, 0.8, 0.7]), points)
    assert score == 0.8
    assert selected[9, 9]
    assert selected[14, 14]


def test_median_rgb_growth_connects_seed_through_matching_color() -> None:
    image = np.zeros((20, 30, 3), dtype=np.uint8)
    image[8:12, 5:25] = [40, 240, 70]
    image[9, 14:16] = [45, 230, 75]
    seed = np.zeros((20, 30), dtype=bool)
    seed[8:12, 5:10] = True
    seed[8:12, 20:25] = True
    refined, median = refine_mask_by_median_rgb(
        image,
        seed,
        CropBounds(5, 2, 15, 28),
        tolerance_fraction=0.10,
        dilation_radius=0,
    )
    assert median == (40, 240, 70)
    assert refined[9, 15]
    assert int(refined.sum()) == 80


def test_rgb_growth_does_not_leave_crop_and_dilates_radius_two() -> None:
    image = np.full((15, 15, 3), [20, 220, 40], dtype=np.uint8)
    seed = np.zeros((15, 15), dtype=bool)
    seed[7, 7] = True
    crop = CropBounds(5, 5, 10, 10)
    refined, _ = refine_mask_by_median_rgb(
        image, seed, crop, dilation_radius=2
    )
    assert np.all(refined[:5] == 0)
    assert np.all(refined[:, :5] == 0)
    assert refined[5:10, 5:10].all()


def test_median_lab_growth_uses_euclidean_distance_and_seed_connectivity() -> None:
    image = np.full((12, 18, 3), [240, 240, 240], dtype=np.uint8)
    image[4:8, 2:13] = [35, 28, 42]
    image[4:8, 15:17] = [35, 28, 42]
    seed = np.zeros((12, 18), dtype=bool)
    seed[5:7, 2:5] = True
    refined, median = refine_mask_by_median_lab(
        image,
        seed,
        CropBounds(2, 1, 10, 17),
        distance_threshold=30.0,
        dilation_radius=0,
    )
    expected_lab = cv2.cvtColor(
        np.asarray([[[35, 28, 42]]], dtype=np.uint8), cv2.COLOR_RGB2LAB
    )[0, 0]
    assert median == tuple(int(value) for value in expected_lab)
    assert refined[5, 12]
    assert not refined[5, 15]
    assert int(refined.sum()) == 44


def test_lab_growth_does_not_leave_crop_and_dilates_radius_two() -> None:
    image = np.full((15, 15, 3), [35, 28, 42], dtype=np.uint8)
    seed = np.zeros((15, 15), dtype=bool)
    seed[7, 7] = True
    crop = CropBounds(5, 5, 10, 10)
    refined, _ = refine_mask_by_median_lab(
        image, seed, crop, distance_threshold=30.0, dilation_radius=2
    )
    assert np.all(refined[:5] == 0)
    assert np.all(refined[:, :5] == 0)
    assert refined[5:10, 5:10].all()
