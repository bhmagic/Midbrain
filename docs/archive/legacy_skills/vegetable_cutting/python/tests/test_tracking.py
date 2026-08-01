from __future__ import annotations

import numpy as np

from vegetable_cutting.tracking import (
    AppearanceTracker,
    evaluate_tracking,
    mask_iou,
)


TRACKING_CONFIG = {
    "minimum_mask_iou": 0.55,
    "maximum_centroid_shift_mm": 4.0,
    "maximum_axis_change_deg": 4.0,
    "minimum_confidence": 0.7,
    "minimum_valid_depth_fraction": 0.65,
}


def test_tracking_passes_without_vlm_when_geometry_is_stable() -> None:
    decision = evaluate_tracking(
        mask_iou_value=0.92,
        centroid_shift_mm=1.2,
        axis_change_deg=1.5,
        confidence=0.9,
        valid_depth_fraction=0.95,
        config=TRACKING_CONFIG,
    )
    assert decision.accepted_without_vlm is True
    assert decision.request_vlm is False


def test_tracking_requests_vlm_only_after_threshold_failure() -> None:
    decision = evaluate_tracking(
        mask_iou_value=0.4,
        centroid_shift_mm=7.0,
        axis_change_deg=1.0,
        confidence=0.8,
        valid_depth_fraction=0.9,
        config=TRACKING_CONFIG,
    )
    assert decision.request_vlm is True
    assert "mask_iou_below_limit" in decision.reasons
    assert "centroid_shift_above_limit" in decision.reasons


def test_appearance_tracker_recovers_static_colored_workpiece() -> None:
    rgb = np.full((80, 120, 3), [80, 70, 60], dtype=np.uint8)
    rgb[25:55, 35:90] = [45, 190, 70]
    mask = np.zeros((80, 120), dtype=np.uint8)
    mask[25:55, 35:90] = 255
    tracker = AppearanceTracker(rgb, mask, lab_distance_threshold=28.0)
    output, confidence = tracker.segment(rgb)
    assert confidence > 0.9
    assert mask_iou(mask, output) > 0.95


def test_tracker_baseline_is_stable_after_seed_refinement() -> None:
    rgb = np.full((90, 140, 3), [210, 205, 195], dtype=np.uint8)
    rgb[30:58, 42:112] = [35, 170, 55]
    seed = np.zeros((90, 140), dtype=np.uint8)
    seed[24:70, 35:95] = 255
    seed_tracker = AppearanceTracker(rgb, seed, lab_distance_threshold=28.0)
    baseline, baseline_confidence = seed_tracker.segment(rgb)
    tracker = AppearanceTracker(rgb, baseline, lab_distance_threshold=28.0)
    repeated, repeated_confidence = tracker.segment(rgb)
    assert baseline_confidence > 0.5
    assert repeated_confidence > 0.95
    assert mask_iou(baseline, repeated) > 0.98
