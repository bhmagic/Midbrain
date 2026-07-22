from __future__ import annotations

import json

from foundation_pose_provider.openai_detector import (
    detection_schema,
    extract_output_text,
    parse_detections,
)


def test_parse_structured_detection() -> None:
    detections = parse_detections(
        json.dumps(
            {
                "detections": [
                    {
                        "model_id": "robot_arm_root",
                        "label": "base",
                        "box_2d": [10, 20, 400, 500],
                        "positive_points_2d": [[100, 100], [300, 300]],
                    }
                ]
            }
        )
    )
    assert detections["robot_arm_root"].detector_id == "openai"


def test_extract_output_text() -> None:
    payload = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "{}"}]}
        ]
    }
    assert extract_output_text(payload) == "{}"


def test_strict_schema_disallows_extra_properties() -> None:
    schema = detection_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["detections"]["items"]["additionalProperties"] is False
