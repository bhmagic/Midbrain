from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class TrackingDecision:
    accepted_without_vlm: bool
    request_vlm: bool
    reasons: list[str]
    confidence: float

    def payload(self) -> dict[str, Any]:
        return {
            "accepted_without_vlm": self.accepted_without_vlm,
            "request_vlm": self.request_vlm,
            "reasons": list(self.reasons),
            "confidence": float(self.confidence),
        }


def mask_geometry(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.nonzero(mask > 0)
    if len(xx) < 8:
        raise RuntimeError("tracking mask has too few pixels")
    points = np.column_stack([xx.astype(np.float64), yy.astype(np.float64)])
    center = np.mean(points, axis=0)
    covariance = np.cov((points - center).T)
    values, vectors = np.linalg.eigh(covariance)
    axis = vectors[:, int(np.argmax(values))]
    if axis[0] < 0:
        axis = -axis
    return center, axis / np.linalg.norm(axis)


def mask_iou(reference: np.ndarray, current: np.ndarray) -> float:
    a = reference > 0
    b = current > 0
    union = int(np.count_nonzero(a | b))
    return float(np.count_nonzero(a & b) / max(union, 1))


def axis_delta_deg(reference_axis: np.ndarray, current_axis: np.ndarray) -> float:
    cosine = float(np.clip(abs(np.dot(reference_axis, current_axis)), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def evaluate_tracking(
    *,
    mask_iou_value: float,
    centroid_shift_mm: float,
    axis_change_deg: float,
    confidence: float,
    valid_depth_fraction: float,
    config: dict[str, Any],
) -> TrackingDecision:
    reasons: list[str] = []
    if mask_iou_value < float(config["minimum_mask_iou"]):
        reasons.append("mask_iou_below_limit")
    if centroid_shift_mm > float(config["maximum_centroid_shift_mm"]):
        reasons.append("centroid_shift_above_limit")
    if axis_change_deg > float(config["maximum_axis_change_deg"]):
        reasons.append("axis_change_above_limit")
    if confidence < float(config["minimum_confidence"]):
        reasons.append("tracking_confidence_below_limit")
    if valid_depth_fraction < float(config["minimum_valid_depth_fraction"]):
        reasons.append("valid_depth_fraction_below_limit")
    return TrackingDecision(
        accepted_without_vlm=not reasons,
        request_vlm=bool(reasons),
        reasons=reasons,
        confidence=float(confidence),
    )


class AppearanceTracker:
    def __init__(
        self,
        reference_rgb: np.ndarray,
        reference_mask: np.ndarray,
        *,
        lab_distance_threshold: float,
    ):
        self.reference_mask = (reference_mask > 0).astype(np.uint8) * 255
        lab = cv2.cvtColor(reference_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        pixels = lab[self.reference_mask > 0]
        if pixels.shape[0] < 32:
            raise RuntimeError("reference vegetable mask is too small")
        self.median_lab = np.median(pixels, axis=0)
        self.scale_lab = np.maximum(np.median(np.abs(pixels - self.median_lab), axis=0), 3.0)
        self.distance_threshold = float(lab_distance_threshold)

    def segment(self, rgb: np.ndarray) -> tuple[np.ndarray, float]:
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        normalized = (lab - self.median_lab) / self.scale_lab
        distance = np.linalg.norm(normalized, axis=2)
        candidate = (distance <= self.distance_threshold / 10.0).astype(np.uint8) * 255
        candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        candidate = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        labels_count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate)
        if labels_count <= 1:
            return np.zeros_like(candidate), 0.0
        overlap_scores: list[tuple[int, int, int]] = []
        for label in range(1, labels_count):
            overlap = int(np.count_nonzero((labels == label) & (self.reference_mask > 0)))
            area = int(stats[label, cv2.CC_STAT_AREA])
            overlap_scores.append((overlap, area, label))
        _, _, selected = max(overlap_scores)
        output = np.zeros_like(candidate)
        output[labels == selected] = 255
        area_ratio = float(stats[selected, cv2.CC_STAT_AREA] / max(np.count_nonzero(self.reference_mask), 1))
        confidence = float(np.clip(1.0 - abs(1.0 - area_ratio), 0.0, 1.0))
        return output, confidence
