from __future__ import annotations

import numpy as np

from sam2_scene_tracker.prompts import parse_visual_prompts
from sam2_scene_tracker.sam_backend import prompt_from_mask


def test_vlm_prompt_parser_accepts_multiple_regions_for_one_object() -> None:
    grouped = parse_visual_prompts(
        {
            "detections": [
                {
                    "object_id": "table",
                    "region_id": "wood",
                    "box_2d": [100, 100, 800, 900],
                    "positive_points_2d": [[500, 300], [500, 700]],
                    "confidence": 0.9,
                },
                {
                    "object_id": "table",
                    "region_id": "mat",
                    "box_2d": [300, 300, 700, 700],
                    "positive_points_2d": [[400, 400], [600, 600]],
                    "confidence": 0.8,
                },
            ]
        },
        expected_object_ids={"table"},
    )

    assert len(grouped["table"]) == 2


def test_previous_mask_builds_short_range_tracking_prompt() -> None:
    mask = np.zeros((100, 200), dtype=bool)
    mask[20:80, 40:160] = True
    prompt = prompt_from_mask("table", mask)

    assert prompt.object_id == "table"
    assert prompt.region_id == "sam2-short-range-track"
    assert prompt.box_yxyx[0] < 200
    assert prompt.box_yxyx[2] > 800
