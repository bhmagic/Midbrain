from __future__ import annotations

from typing import Any

import numpy as np

from sam2_scene_tracker.annotator import RoutedSceneAnnotator, build_scene_annotator
from sam2_scene_tracker.policy import parse_policy


class _Backend:
    def __init__(
        self,
        backend_id: str,
        model_id: str,
        result: dict[str, Any] | Exception,
    ) -> None:
        self.backend_id = backend_id
        self.model_id = model_id
        self.result = result
        self.calls = 0

    def generate(
        self,
        image_jpeg: bytes,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        assert image_jpeg.startswith(b"\xff\xd8")
        assert "the table" in prompt
        assert "__robot_arm_self__" in schema["properties"]["detections"][
            "items"
        ]["properties"]["object_id"]["enum"]
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        return None


class _QualityBackend(_Backend):
    def generate(self, image_jpeg, prompt, schema):
        self.calls += 1
        assert image_jpeg.startswith(b"\xff\xd8")
        assert "three panels" in prompt
        object_ids = schema["properties"]["object_results"]["items"][
            "properties"
        ]["object_id"]["enum"]
        if isinstance(self.result, Exception):
            raise self.result
        assert set(object_ids) == {"__robot_arm_self__", "table"}
        return self.result


def _policy():
    return parse_policy(
        {
            "contract_version": 1,
            "policy_id": "test",
            "objects": [
                {
                    "object_id": "table",
                    "type": "KEEP_OUT",
                    "description": "the table",
                }
            ],
            "arm_description": "the complete robot arm",
        }
    )


def test_scene_vlm_pool_prefers_er2_and_falls_back() -> None:
    er2 = _Backend(
        "google.gemini",
        "gemini-robotics-er-2-preview",
        RuntimeError("temporary ER2 failure"),
    )
    fallback = _Backend(
        "openai.responses",
        "gpt-5.6-luna",
        {
            "detections": [
                {
                    "object_id": "table",
                    "region_id": "table-1",
                    "box_2d": [100, 100, 900, 900],
                    "positive_points_2d": [[600, 300], [600, 700]],
                    "confidence": 0.9,
                }
            ]
        },
    )
    annotator = RoutedSceneAnnotator([er2, fallback])

    result = annotator.annotate(np.zeros((64, 96, 3), dtype=np.uint8), _policy())

    assert list(result) == ["table"]
    assert er2.calls == 1
    assert fallback.calls == 1
    assert annotator.last_result is not None
    assert annotator.last_result["model_id"] == "gpt-5.6-luna"
    assert annotator.last_result["failed_candidates"][0]["model_id"] == (
        "gemini-robotics-er-2-preview"
    )


def test_scene_vlm_pool_stops_after_preferred_er2_succeeds() -> None:
    payload = {
        "detections": [
            {
                "object_id": "table",
                "region_id": "table-1",
                "box_2d": [100, 100, 900, 900],
                "positive_points_2d": [[600, 300], [600, 700]],
                "confidence": 0.9,
            }
        ]
    }
    er2 = _Backend("google.gemini", "gemini-robotics-er-2-preview", payload)
    fallback = _Backend("openai.responses", "gpt-5.6-luna", payload)
    annotator = RoutedSceneAnnotator([er2, fallback])

    annotator.annotate(np.zeros((64, 96, 3), dtype=np.uint8), _policy())

    assert er2.calls == 1
    assert fallback.calls == 0
    assert annotator.describe()["last_result"]["model_id"] == (
        "gemini-robotics-er-2-preview"
    )


def test_scene_vlm_pool_reads_replaceable_model_ids_from_environment() -> None:
    annotator = build_scene_annotator(
        {
            "vlm_candidates": [
                {
                    "backend": "google.gemini",
                    "model_env": "GEMINI_ROBOTICS_MODEL",
                    "model": "fallback-model",
                }
            ]
        },
        {
            "GEMINI_API_KEY": "test-key",
            "GEMINI_ROBOTICS_MODEL": "gemini-robotics-er-2-preview",
        },
    )
    try:
        assert annotator.describe()["ordered_candidates"] == [
            {
                "backend_id": "google.gemini",
                "model_id": "gemini-robotics-er-2-preview",
            }
        ]
    finally:
        annotator.close()


def test_vlm_reviews_sam2_masks_before_fusion() -> None:
    backend = _QualityBackend(
        "google.gemini",
        "gemini-robotics-er-2-preview",
        {
            "accepted": True,
            "object_results": [
                {
                    "object_id": "__robot_arm_self__",
                    "acceptable": True,
                    "problem": "NONE",
                },
                {
                    "object_id": "table",
                    "acceptable": True,
                    "problem": "NONE",
                },
            ],
        },
    )
    annotator = RoutedSceneAnnotator([backend])
    masks = {
        "__robot_arm_self__": np.zeros((32, 48), dtype=bool),
        "table": np.zeros((32, 48), dtype=bool),
    }
    masks["__robot_arm_self__"][:, 20:24] = True
    masks["table"][20:, :] = True

    result = annotator.validate_masks(
        np.zeros((32, 48, 3), dtype=np.uint8),
        np.full((32, 48), 0.8, dtype=np.float32),
        masks,
        _policy(),
    )

    assert result["accepted"] is True
    assert result["model_id"] == "gemini-robotics-er-2-preview"
    assert backend.calls == 1
