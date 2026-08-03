from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jsonschema import validate

from physical_agent_test.gemini_pointing_skill import (
    PointingIdentificationSkill,
)
from physical_agent_test.rgb_capture import CameraObservationUnavailable
from physical_agent_test.visual_evidence import (
    VisualEvidenceStore,
    sanitize_visual_evidence,
)


class VisualEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_returns_schema_valid_exact_rgb_reference(self) -> None:
        store = VisualEvidenceStore()
        evidence = await store.register_rgb(
            image_bytes=b"exact-rgb-bytes",
            media_type="image/jpeg",
            width=640,
            height=480,
            title="Pointing identification",
            annotations=[
                {
                    "id": "target-1",
                    "type": "box",
                    "label": "target",
                    "confidence": "high",
                    "applies_to_channels": ["rgb"],
                    "x": 0.25,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                }
            ],
            confidence="high",
            model="vlm-test",
            source_skill="test.skill",
        )

        schema_path = (
            Path(__file__).resolve().parents[3]
            / "contracts"
            / "schemas"
            / "visual_evidence.v1.schema.json"
        )
        validate(
            instance=evidence,
            schema=json.loads(schema_path.read_text(encoding="utf-8")),
        )
        channel = await store.read(evidence["evidence_id"], "rgb")
        self.assertEqual(channel.data, b"exact-rgb-bytes")
        self.assertEqual(channel.width, 640)
        self.assertIn(evidence["evidence_id"], evidence["channels"][0]["url"])

    async def test_browser_projection_rejects_arbitrary_image_urls(self) -> None:
        store = VisualEvidenceStore()
        evidence = await store.register_rgb(
            image_bytes=b"rgb",
            media_type="image/jpeg",
            width=10,
            height=10,
            title="Evidence",
            annotations=[],
            confidence="unknown",
            model="test",
            source_skill="test.skill",
        )
        evidence["channels"][0]["url"] = "https://example.test/private"

        self.assertIsNone(sanitize_visual_evidence(evidence))

    def test_pointing_result_parses_only_supported_normalized_geometry(
        self,
    ) -> None:
        parsed = PointingIdentificationSkill._parse_visual_result(
            json.dumps(
                {
                    "answer": "The person points at the red bin.",
                    "confidence": "high",
                    "annotations": [
                        {
                            "type": "point",
                            "x": 0.2,
                            "y": 0.4,
                            "label": "fingertip",
                            "confidence": "medium",
                        },
                        {
                            "type": "box",
                            "x": 0.5,
                            "y": 0.2,
                            "width": 0.3,
                            "height": 0.5,
                            "label": "red bin",
                            "confidence": "high",
                        },
                        {
                            "type": "box",
                            "x": 0.9,
                            "y": 0.9,
                            "width": 0.5,
                            "height": 0.5,
                            "label": "invalid overflow",
                        },
                        {
                            "type": "raw_svg",
                            "markup": "<script>unsafe()</script>",
                        },
                    ],
                }
            )
        )

        self.assertEqual(parsed["confidence"], "high")
        self.assertEqual(len(parsed["annotations"]), 2)
        self.assertEqual(
            [item["type"] for item in parsed["annotations"]],
            ["point", "box"],
        )
        self.assertEqual(
            parsed["annotation_processing"]["coordinate_spaces"],
            {"normalized_0_1": 2},
        )
        self.assertEqual(
            parsed["annotation_processing"]["rejected_count"],
            2,
        )
        self.assertNotIn("unsafe", str(parsed))

    def test_robotics_er_coordinates_are_normalized_at_vlm_boundary(
        self,
    ) -> None:
        parsed = PointingIdentificationSkill._parse_visual_result(
            json.dumps(
                {
                    "answer": "The finger points at the bottle.",
                    "confidence": "high",
                    "annotations": [
                        {
                            "type": "point",
                            "x": 589,
                            "y": 868,
                            "label": "fingertip",
                            "confidence": "high",
                        },
                        {
                            "type": "box",
                            "x": 522,
                            "y": 796,
                            "width": 54,
                            "height": 131,
                            "label": "bottle",
                            "confidence": "high",
                        },
                    ],
                }
            ),
            image_width=1920,
            image_height=1080,
            coordinate_hint="normalized_0_1000",
        )

        point, box = parsed["annotations"]
        self.assertAlmostEqual(point["x"], 0.589)
        self.assertAlmostEqual(point["y"], 0.868)
        self.assertAlmostEqual(box["x"], 0.522)
        self.assertAlmostEqual(box["y"], 0.796)
        self.assertAlmostEqual(box["width"], 0.054)
        self.assertAlmostEqual(box["height"], 0.131)
        self.assertEqual(
            parsed["annotation_processing"]["coordinate_spaces"],
            {"normalized_0_1000": 2},
        )
        self.assertEqual(
            parsed["annotation_processing"]["accepted_count"],
            2,
        )

    def test_explicit_pixel_coordinates_use_captured_image_dimensions(
        self,
    ) -> None:
        parsed = PointingIdentificationSkill._parse_visual_result(
            json.dumps(
                {
                    "answer": "Pixel-localized target.",
                    "coordinate_space": "pixels",
                    "annotations": [
                        {
                            "type": "box",
                            "x": 960,
                            "y": 270,
                            "width": 480,
                            "height": 540,
                            "label": "target",
                        }
                    ],
                }
            ),
            image_width=1920,
            image_height=1080,
        )

        self.assertEqual(
            {
                key: parsed["annotations"][0][key]
                for key in ("x", "y", "width", "height")
            },
            {"x": 0.5, "y": 0.25, "width": 0.25, "height": 0.5},
        )
        self.assertEqual(
            parsed["annotation_processing"]["coordinate_spaces"],
            {"pixels": 1},
        )

    def test_ambiguous_large_coordinates_are_rejected_with_diagnostics(
        self,
    ) -> None:
        parsed = PointingIdentificationSkill._parse_visual_result(
            json.dumps(
                {
                    "answer": "Unlabeled coordinate space.",
                    "annotations": [
                        {"type": "point", "x": 400, "y": 300},
                    ],
                }
            ),
            image_width=1920,
            image_height=1080,
        )

        self.assertEqual(parsed["annotations"], [])
        self.assertEqual(
            parsed["annotation_processing"]["rejections"],
            [
                {
                    "index": 0,
                    "type": "point",
                    "reason": "missing_coordinate_space",
                }
            ],
        )

    async def test_visual_skill_registers_the_exact_inference_frame(self) -> None:
        store = VisualEvidenceStore()
        captured = SimpleNamespace(
            image_bytes=b"captured-frame",
            mime_type="image/jpeg",
            path=Path("diagnostic.jpg"),
            observation={
                "frame_id": "camera_color_optical_frame",
                "data": {"width": 320, "height": 240},
            },
            data_route=None,
        )
        capture = SimpleNamespace(capture_latest=lambda **_kwargs: None)

        async def capture_latest(**_kwargs):
            return captured

        capture.capture_latest = capture_latest
        inference = SimpleNamespace(
            text=json.dumps(
                {
                    "answer": "Target object",
                    "confidence": "medium",
                    "annotations": [
                        {
                            "type": "point",
                            "x": 250,
                            "y": 750,
                            "label": "target",
                        }
                    ],
                }
            ),
            model_id="gemini-robotics-er-2-preview",
            as_dict=lambda: {"model_id": "gemini-robotics-er-2-preview"},
        )
        router = SimpleNamespace(generate=lambda **_kwargs: None)

        async def generate(**_kwargs):
            return inference

        router.generate = generate
        skill = PointingIdentificationSkill(
            capture=capture,
            model="vlm-test",
            manager=None,
            vlm_router=router,
            visual_evidence_store=store,
        )

        result = json.loads(await skill.run("Which object?"))

        evidence = result["visual_evidence"]
        stored = await store.read(evidence["evidence_id"], "rgb")
        self.assertEqual(stored.data, b"captured-frame")
        self.assertEqual((stored.width, stored.height), (320, 240))
        self.assertEqual(result["annotations"][0]["x"], 0.25)
        self.assertEqual(result["annotations"][0]["y"], 0.75)
        self.assertEqual(
            evidence["annotations"],
            result["annotations"],
        )
        self.assertEqual(
            result["annotation_processing"]["coordinate_spaces"],
            {"normalized_0_1000": 1},
        )
        self.assertNotIn("diagnostic.jpg", json.dumps(evidence))

    async def test_visual_skill_reports_retryable_capture_timeout(self) -> None:
        capture = SimpleNamespace(capture_latest=lambda **_kwargs: None)
        capture_calls = 0

        async def capture_latest(**_kwargs):
            nonlocal capture_calls
            capture_calls += 1
            raise CameraObservationUnavailable("bounded timeout")

        capture.capture_latest = capture_latest
        skill = PointingIdentificationSkill(
            capture=capture,
            model="vlm-test",
            manager=None,
            capture_retry_backoff_s=0,
        )

        result = json.loads(await skill.run("What is visible?"))

        self.assertEqual(result["status"], "CAMERA_FRAME_UNAVAILABLE")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_scope"], "CAPTURE_RGB_ONLY")
        self.assertFalse(result["physical_action_submitted"])
        self.assertEqual(capture_calls, 2)
        self.assertEqual(
            result["retry"]["classification"],
            "transient_observation",
        )
        self.assertEqual(result["retry_history"]["attempt_count"], 2)
        self.assertTrue(result["retry_history"]["exhausted"])
        self.assertFalse(
            result["retry_history"]["physical_action_submitted"]
        )

    async def test_visual_skill_recovers_at_capture_boundary(self) -> None:
        capture_calls = 0
        captured = SimpleNamespace(
            image_bytes=b"recovered-frame",
            mime_type="image/jpeg",
            path=Path("recovered.jpg"),
            observation={
                "frame_id": "camera_color_optical_frame",
                "data": {"width": 320, "height": 240},
            },
            data_route=None,
        )
        capture = SimpleNamespace(capture_latest=lambda **_kwargs: None)

        async def capture_latest(**_kwargs):
            nonlocal capture_calls
            capture_calls += 1
            if capture_calls == 1:
                raise CameraObservationUnavailable("first frame was late")
            return captured

        capture.capture_latest = capture_latest
        inference_calls = 0
        inference = SimpleNamespace(
            text=json.dumps(
                {
                    "answer": "Recovered target",
                    "confidence": "high",
                    "annotations": [],
                }
            ),
            model_id="gemini-robotics-er-2-preview",
            as_dict=lambda: {"model_id": "gemini-robotics-er-2-preview"},
        )
        router = SimpleNamespace(generate=lambda **_kwargs: None)

        async def generate(**_kwargs):
            nonlocal inference_calls
            inference_calls += 1
            return inference

        router.generate = generate
        skill = PointingIdentificationSkill(
            capture=capture,
            model="vlm-test",
            manager=None,
            vlm_router=router,
            capture_retry_backoff_s=0,
        )

        result = json.loads(await skill.run("What is visible?"))

        self.assertEqual(result["answer"], "Recovered target")
        self.assertEqual(capture_calls, 2)
        self.assertEqual(inference_calls, 1)
        self.assertTrue(result["retry_history"]["recovered"])
        self.assertFalse(result["retry_history"]["exhausted"])
        self.assertEqual(
            [item["outcome"] for item in result["retry_history"]["attempts"]],
            ["camera_frame_unavailable", "succeeded"],
        )


if __name__ == "__main__":
    unittest.main()
