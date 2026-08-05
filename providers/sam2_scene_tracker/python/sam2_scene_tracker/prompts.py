from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ARM_OBJECT_ID = "__robot_arm_self__"


@dataclass(frozen=True)
class VisualPrompt:
    object_id: str
    region_id: str
    box_yxyx: tuple[int, int, int, int]
    positive_points_yx: tuple[tuple[int, int], tuple[int, int]]
    confidence: float


def parse_visual_prompts(
    payload: dict[str, Any],
    *,
    expected_object_ids: set[str],
) -> dict[str, list[VisualPrompt]]:
    detections = payload.get("detections")
    if not isinstance(detections, list):
        raise ValueError("scene annotation requires a detections list")
    grouped: dict[str, list[VisualPrompt]] = {}
    for index, value in enumerate(detections):
        if not isinstance(value, dict):
            raise ValueError(f"scene detection {index} must be an object")
        object_id = str(value.get("object_id") or "").strip()
        if object_id not in expected_object_ids:
            raise ValueError(f"unexpected scene detection object_id {object_id!r}")
        box = value.get("box_2d")
        points = value.get("positive_points_2d")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not isinstance(points, list)
            or len(points) != 2
            or any(not isinstance(point, list) or len(point) != 2 for point in points)
        ):
            raise ValueError("scene detection box/positive points are invalid")
        normalized_box = tuple(int(item) for item in box)
        normalized_points = tuple(
            (int(point[0]), int(point[1])) for point in points
        )
        if any(item < 0 or item > 1000 for item in normalized_box) or any(
            item < 0 or item > 1000
            for point in normalized_points
            for item in point
        ):
            raise ValueError("scene detection coordinates must be in [0, 1000]")
        y0, x0, y1, x1 = normalized_box
        if y1 <= y0 or x1 <= x0:
            raise ValueError("scene detection box must have positive area")
        confidence = float(value.get("confidence") or 0.0)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("scene detection confidence must be in [0, 1]")
        region_id = str(value.get("region_id") or f"region-{index}").strip()
        grouped.setdefault(object_id, []).append(
            VisualPrompt(
                object_id=object_id,
                region_id=region_id,
                box_yxyx=normalized_box,
                positive_points_yx=normalized_points,  # type: ignore[arg-type]
                confidence=confidence,
            )
        )
    return grouped
