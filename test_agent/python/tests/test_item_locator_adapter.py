from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import numpy as np

from physical_agent_test.item_locator_adapter import (
    MetricItemLocatorAdapter,
    build_item_locator_evidence,
    build_item_locator_visual_channels,
    evaluate_item_visual_support,
)
from physical_agent_test.visual_evidence import VisualEvidenceStore
from physical_agent_test.vlm_router import VlmInferenceResult


class _Snapshot:
    def __init__(self, identity: str):
        self.identity = identity

    def as_dict(self):
        return {"identity": self.identity}


class _Spatial:
    binding_mode = "ENFORCED"
    generic_route_mode = "ENFORCED"

    def __init__(self):
        rgb = np.full((8, 12, 3), 20, dtype=np.uint8)
        rgb[2:7, 3:10] = 220
        frame = SimpleNamespace(
            rgb=rgb,
            depth_m=np.full((8, 12), 0.7, dtype=np.float32),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.5, "cy": 3.5},
            timestamp_us=time.time_ns() // 1000,
            frame_number=11,
            camera_frame="camera_registered_depth",
            session_epoch="vio-epoch",
            world_frame="stationary_world",
            calibration_revision="calibration-1",
        )
        self.context = SimpleNamespace(
            frame=frame,
            valid_region={"x": 0, "y": 0, "width": 12, "height": 8},
            binding=_Snapshot("capture-binding"),
            selection=_Snapshot("generic-route"),
            target_from_camera=np.eye(4),
            temporal_evidence={"bundle": {"accepted": True}},
        )

    async def prepare_context(self, *, target_frame, skill_id):
        self.context.target_frame = target_frame
        self.context.skill_id = skill_id
        return self.context

    async def revalidate_context_binding(self, _context):
        return _Snapshot("current-binding")

    def capture_provenance(self, _context):
        return {"frame_number": 11}

    def transform_provenance(self, _context):
        return {"at_us": self.context.frame.timestamp_us}

    def route_metadata(self, _context):
        return {"valid_region": self.context.valid_region}


class _Router:
    async def generate(self, *, image_bytes, mime_type, prompt):
        if not image_bytes.startswith(b"\x89PNG"):
            raise AssertionError("item evidence must be a PNG")
        if mime_type != "image/png":
            raise AssertionError("item evidence MIME type is wrong")
        if "NORMALIZED_0_1000" not in prompt:
            raise AssertionError("normalized coordinate contract is missing")
        return VlmInferenceResult(
            text=(
                '{"schema":"physical_agent.item_landmark_vlm",'
                '"schema_version":2,'
                '"coordinate_space":"NORMALIZED_0_1000",'
                '"scene_suitable":true,'
                '"reason":"visible white roll","item_label":"toilet paper roll",'
                '"confidence":0.95,"material_class":"OPAQUE_DIFFUSE",'
                '"registered_depth_pixel_yx":[571,545],'
                '"registered_depth_box_yxyx":[250,250,875,833],'
                '"same_surface_search_allowed":true}'
            ),
            backend_id="vlm.test",
            model_id="test-model",
            attempt_count=1,
            failed_attempts=(),
            quality_control_mode="OFF_FUTURE",
            elapsed_ms=5.0,
            input_sha256="hash",
            input_bytes=len(image_bytes),
            mime_type=mime_type,
        )


class _Publisher:
    def __init__(self):
        self.results = []

    async def publish_item_location(self, result):
        self.results.append(result)
        return {
            "status": "PUBLISHED",
            "object_id": result["object_id"],
            "type": "WORKPIECE",
        }


class ItemLocatorAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_item_is_registered_without_motion(self):
        adapter = MetricItemLocatorAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        result = await adapter.run(
            question="locate the toilet paper roll",
            target_frame="rebot_arm_base",
            object_id="toilet-paper-roll",
            contact_policy="NO_CONTACT",
        )

        self.assertEqual(result["status"], "METRIC_POINT_READY")
        self.assertTrue(result["eligible_for_control_math"])
        self.assertEqual(result["target_frame"], "rebot_arm_base")
        self.assertEqual(result["object_id"], "toilet-paper-roll")
        self.assertEqual(result["contact_policy"], "NO_CONTACT")
        self.assertFalse(result["physical_action_submitted"])
        self.assertFalse(result["control_frame_published"])
        self.assertEqual(result["vlm_route"]["backend_id"], "vlm.test")
        self.assertEqual(
            result["vlm_geometry"]["source_pixel_yx"],
            [571, 545],
        )
        self.assertEqual(
            result["vlm_geometry"]["registered_depth_pixel_yx"],
            [4, 6],
        )

    async def test_question_and_target_frame_are_required(self):
        adapter = MetricItemLocatorAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(ValueError, "question"):
            await adapter.run(question="", target_frame="rebot_arm_base")
        with self.assertRaisesRegex(ValueError, "target_frame"):
            await adapter.run(question="locate the roll", target_frame="")

    async def test_metric_result_publishes_workpiece_assertion(self):
        publisher = _Publisher()
        adapter = MetricItemLocatorAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
            semantic_assertion_publisher=publisher,
        )

        result = await adapter.run(
            question="locate the toilet paper roll",
            target_frame="rebot_arm_base",
            object_id="toilet-paper-roll",
        )

        self.assertEqual(
            result["semantic_scene_assertion"]["status"],
            "PUBLISHED",
        )
        self.assertEqual(len(publisher.results), 1)

    async def test_rejected_retry_preserves_last_trusted_metric_marker(self):
        spatial = _Spatial()
        adapter = MetricItemLocatorAdapter(
            spatial,  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )
        accepted = await adapter.run(
            question="locate the toilet paper roll",
            target_frame="rebot_arm_base",
            object_id="toilet-paper-roll",
        )
        spatial.context.frame.rgb[:] = 20

        rejected = await adapter.run(
            question="locate the toilet paper roll",
            target_frame="rebot_arm_base",
            object_id="toilet-paper-roll",
        )

        self.assertEqual(rejected["status"], "REJECTED_OBSERVATION")
        self.assertIs(adapter.last_result, rejected)
        self.assertIs(adapter.last_metric_result, accepted)

    async def test_metric_result_registers_annotated_visual_evidence(self):
        store = VisualEvidenceStore()
        adapter = MetricItemLocatorAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
            visual_evidence_store=store,
        )

        result = await adapter.run(
            question="locate the toilet paper roll",
            target_frame="rebot_arm_base",
            object_id="toilet-paper-roll",
        )

        visual = result["visual_evidence"]
        self.assertEqual(visual["source_skill"], "locate_item")
        self.assertEqual(visual["confidence"], "high")
        self.assertEqual(
            [channel["id"] for channel in visual["channels"]],
            ["rgb", "depth", "rgb_depth"],
        )
        self.assertEqual(len(visual["annotations"]), 2)
        self.assertEqual(
            {annotation["id"] for annotation in visual["annotations"]},
            {"item-box", "item-requested"},
        )
        channel = await store.read(visual["evidence_id"], "rgb")
        self.assertTrue(channel.data.startswith(b"\x89PNG"))
        self.assertEqual(channel.width, 12)
        self.assertEqual(channel.height, 8)
        depth = await store.read(visual["evidence_id"], "depth")
        overlay = await store.read(visual["evidence_id"], "rgb_depth")
        self.assertTrue(depth.data.startswith(b"\x89PNG"))
        self.assertTrue(overlay.data.startswith(b"\x89PNG"))
        self.assertEqual(
            set(visual["annotations"][0]["applies_to_channels"]),
            {"rgb", "depth", "rgb_depth"},
        )

    def test_item_evidence_is_one_clean_rgb_depth_grid(self):
        payload, metadata = build_item_locator_evidence(
            np.zeros((8, 12, 3), dtype=np.uint8),
            np.ones((4, 6), dtype=np.float32),
        )

        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(metadata["image_grid"], [4, 6])
        self.assertEqual(
            metadata["image_layout"],
            "SINGLE_RGB_ON_REGISTERED_DEPTH_GRID",
        )
        self.assertTrue(metadata["rgb_resampled_to_registered_depth_grid"])
        self.assertTrue(metadata["depth_not_shown_to_vlm"])

    def test_visual_channels_share_the_registered_depth_grid(self):
        channels = build_item_locator_visual_channels(
            np.full((8, 12, 3), 120, dtype=np.uint8),
            np.linspace(0.4, 1.2, 24, dtype=np.float32).reshape(4, 6),
        )

        self.assertEqual(
            [channel["id"] for channel in channels],
            ["rgb", "depth", "rgb_depth"],
        )
        self.assertTrue(
            all(
                channel["width"] == 6 and channel["height"] == 4
                for channel in channels
            )
        )
        self.assertTrue(
            all(
                channel["image_bytes"].startswith(b"\x89PNG")
                for channel in channels
            )
        )

    def test_uniform_background_fragment_is_rejected(self):
        support = evaluate_item_visual_support(
            np.full((20, 30, 3), 40, dtype=np.uint8),
            np.ones((20, 30), dtype=np.float32),
            vlm_result={
                "scene_suitable": True,
                "registered_depth_pixel_yx": [10, 15],
                "registered_depth_box_yxyx": [5, 12, 16, 18],
            },
        )

        self.assertEqual(support["decision"], "REJECT")
        self.assertEqual(
            support["reason"],
            "BOX_MATCHES_SUPPORT_OR_BACKGROUND",
        )


if __name__ == "__main__":
    unittest.main()
