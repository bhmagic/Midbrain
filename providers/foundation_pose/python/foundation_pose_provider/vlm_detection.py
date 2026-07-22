"""Shared structured detection contract for VLM-guided initialization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .bounding_box import BoundingBoxMask


TARGETS = {
    "robot_arm_root": "reBot B601-DM robot base",
    "robot_gripper_slider_support": "reBot B601-DM gripper slider support",
}


@dataclass(frozen=True)
class NormalizedPoint:
    """A [y, x] image point normalized to the inclusive 0-1000 range."""

    y: float
    x: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.y) or not math.isfinite(self.x):
            raise ValueError("point coordinates must be finite")
        if not 0.0 <= self.y <= 1000.0 or not 0.0 <= self.x <= 1000.0:
            raise ValueError("point coordinates must be normalized to 0-1000")

    @classmethod
    def from_value(cls, value: Any) -> "NormalizedPoint":
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("each positive point must contain [y, x]")
        if len(value) != 2:
            raise ValueError("each positive point must contain [y, x]")
        return cls(float(value[0]), float(value[1]))

    def to_pixel_yx(self, height: int, width: int) -> tuple[float, float]:
        if height <= 0 or width <= 0:
            raise ValueError("image dimensions must be positive")
        return self.y * height / 1000.0, self.x * width / 1000.0

    def public_payload(self) -> list[float]:
        return [self.y, self.x]


@dataclass(frozen=True)
class Detection:
    model_id: str
    label: str
    box: BoundingBoxMask
    positive_points: tuple[NormalizedPoint, NormalizedPoint]
    detector_id: str

    def __post_init__(self) -> None:
        if self.model_id not in TARGETS:
            raise ValueError(f"unsupported detection model_id: {self.model_id}")
        if len(self.positive_points) != 2:
            raise ValueError("each detection must contain exactly two positive points")
        if self.box.coordinate_space != "normalized_0_1000":
            raise ValueError("VLM detections must use normalized_0_1000 boxes")
        ymin, xmin, ymax, xmax = self.box.box_2d
        for point in self.positive_points:
            if not ymin <= point.y <= ymax or not xmin <= point.x <= xmax:
                raise ValueError("positive points must lie inside their bounding box")
        first, second = self.positive_points
        if math.hypot(first.y - second.y, first.x - second.x) < 1.0:
            raise ValueError("positive points must be distinct")


def _canonical_model_id(model_id: str, label: str) -> str:
    if model_id in TARGETS:
        return model_id
    lower_label = label.lower()
    if "gripper" in lower_label or "slider support" in lower_label:
        return "robot_gripper_slider_support"
    if "base" in lower_label or "arm root" in lower_label:
        return "robot_arm_root"
    return model_id


def parse_detection_payload(
    payload: Any,
    *,
    detector_id: str,
) -> dict[str, Detection]:
    if isinstance(payload, Mapping):
        payload = payload.get("detections") or payload.get("boxes") or []
    if not isinstance(payload, list):
        raise ValueError("VLM detection response must contain a detections array")

    detections: dict[str, Detection] = {}
    errors: list[str] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            errors.append(f"item {index} is not an object")
            continue
        raw_model_id = str(item.get("model_id") or "").strip()
        label = str(item.get("label") or raw_model_id).strip()
        model_id = _canonical_model_id(raw_model_id, label)
        if model_id not in TARGETS or model_id in detections:
            continue
        try:
            box = BoundingBoxMask.from_request(
                {
                    "box_2d": item.get("box_2d"),
                    "bounding_box_coordinate_space": "normalized_0_1000",
                }
            )
            if box is None:
                raise ValueError("box_2d is missing")
            raw_points = item.get("positive_points_2d")
            if not isinstance(raw_points, list) or len(raw_points) != 2:
                raise ValueError("positive_points_2d must contain exactly two points")
            points = tuple(NormalizedPoint.from_value(value) for value in raw_points)
            detection = Detection(
                model_id=model_id,
                label=label or TARGETS[model_id],
                box=box,
                positive_points=(points[0], points[1]),
                detector_id=detector_id,
            )
        except (TypeError, ValueError) as error:
            errors.append(f"{model_id or index}: {error}")
            continue
        detections[model_id] = detection

    if not detections:
        detail = "; ".join(errors) if errors else "no recognized targets"
        raise ValueError(f"VLM returned no usable Base or Gripper detection: {detail}")
    return detections
