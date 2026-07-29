from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from physical_agent_test.rgbd_alignment import (
    RgbdAlignmentValidationSkill,
    RgbdEvidence,
    RgbdEvidenceCapture,
    build_alignment_composite,
    decode_depth_m,
    parse_alignment_vlm_result,
    summarize_bundle_cadence,
    valid_depth_boundary,
)
from physical_agent_test.phase4_policy import Phase4Policy


class RgbdAlignmentTests(unittest.TestCase):
    @staticmethod
    def _cadence_observation(
        frame_number: int,
        *,
        rgb_timestamp_us: int,
        depth_timestamp_us: int,
    ) -> dict:
        def reference(
            frame: int,
            timestamp_us: int,
            width: int,
            height: int,
            format_name: str,
        ) -> dict:
            return {
                "frame_number": frame,
                "global_timestamp_us": timestamp_us,
                "width": width,
                "height": height,
                "format_name": format_name,
            }

        return {
            "data": {
                "synchronized": True,
                "max_delta_us": 50_000,
                "rgb": reference(
                    frame_number * 2,
                    rgb_timestamp_us,
                    1280,
                    720,
                    "MJPG",
                ),
                "depth": reference(
                    frame_number,
                    depth_timestamp_us,
                    640,
                    576,
                    "Y16",
                ),
                "depth_aligned_to_rgb": reference(
                    frame_number,
                    rgb_timestamp_us,
                    1280,
                    720,
                    "Y16",
                ),
            }
        }

    def test_depth_decode_honors_stride_and_value_scale(self) -> None:
        values = np.zeros((3, 6), dtype="<u2")
        values[:, :4] = 1500
        depth = decode_depth_m(
            values.tobytes(),
            {
                "format_name": "Y16",
                "width": 4,
                "height": 3,
                "stride_bytes": 12,
                "depth_value_scale_mm": 0.5,
            },
        )
        self.assertEqual(depth.shape, (3, 4))
        self.assertTrue(np.allclose(depth, 0.75))

    def test_boundary_comes_from_nonzero_registered_depth(self) -> None:
        depth = np.zeros((10, 12), dtype=np.float32)
        depth[2:8, 3:11] = 1.2
        self.assertEqual(
            valid_depth_boundary(depth),
            {"x": 3, "y": 2, "width": 8, "height": 6},
        )

    def test_composite_contains_actual_rgb_depth_and_overlay_panels(self) -> None:
        rgb = np.zeros((40, 60, 3), dtype=np.uint8)
        rgb[:, :30, 0] = 255
        depth = np.ones((20, 30), dtype=np.float32)
        payload, metadata = build_alignment_composite(
            rgb,
            depth,
            observed_boundary={"x": 2, "y": 3, "width": 20, "height": 12},
        )
        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.width, 180)
        self.assertGreater(image.height, 40)
        self.assertTrue(metadata["depth_resized_for_display"])
        self.assertEqual(
            metadata["composite_layout"],
            ["RGB", "REGISTERED_DEPTH", "OVERLAY"],
        )

    def test_builtin_review_capture_never_calls_external_vlm(self) -> None:
        class Capture:
            async def capture_latest(
                self,
                *,
                provider_id,
                binding_id,
            ):
                del provider_id, binding_id
                return evidence

        class Router:
            async def generate(self, **_kwargs):
                raise AssertionError("external VLM must not be called")

        policy = Phase4Policy(
            binding="SHADOW",
            controller_audit="ENFORCED",
            manager_authority="SHADOW",
            generic_rgbd_route="SHADOW",
            physical_execution="DISABLED",
            operation_hard_timeout_s=90.0,
            operation_idle_timeout_s=30.0,
            vlm_attempt_timeout_s=45.0,
            skill_adapter_timeout_s=60.0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            composite = b"exact-composite"
            evidence = RgbdEvidence(
                composite_bytes=composite,
                composite_mime_type="image/jpeg",
                composite_path=root / "composite.jpg",
                rgb_path=root / "rgb.jpg",
                aligned_depth_path=root / "depth.png",
                bundle_observation={},
                route={},
                geometry={"rgb_grid": [1080, 1920]},
                timing={"synchronized": True},
                numeric_quality={"motion_usable": True},
            )
            skill = RgbdAlignmentValidationSkill(
                Capture(),
                Router(),
                provider_id="camera.test",
                manager=None,
                policy=policy,
            )
            result = asyncio.run(
                skill.capture_for_builtin_review(
                    "Inspect exact current evidence"
                )
            )
        self.assertEqual(
            result["review_state"],
            "BUILTIN_MULTIMODAL_REVIEW_REQUIRED",
        )
        self.assertFalse(result["motion_usable"])
        self.assertFalse(result["external_vlm_called"])
        self.assertTrue(result["numeric_quality_passed"])

    def test_vlm_alignment_result_requires_content_and_alignment_fields(self) -> None:
        result = parse_alignment_vlm_result(
            json.dumps(
                {
                    "rgb_content_visible": True,
                    "registered_depth_content_visible": True,
                    "same_scene": True,
                    "boundary_consistent": True,
                    "major_misalignment": False,
                    "alignment_quality": "warn",
                    "confidence": "medium",
                    "reason": "Most major edges correspond.",
                }
            )
        )
        self.assertEqual(result["alignment_quality"], "WARN")
        self.assertEqual(result["confidence"], "MEDIUM")

    def test_vlm_cannot_pass_by_returning_only_an_image_description(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "did not return a JSON object"):
            parse_alignment_vlm_result("The image seems to contain a table.")

    def test_cadence_allows_different_channel_rates_and_grids(self) -> None:
        observations = [
            self._cadence_observation(
                frame,
                rgb_timestamp_us=1_000_000 + frame * 66_666,
                depth_timestamp_us=1_010_000 + frame * 66_666,
            )
            for frame in range(4)
        ]
        result = summarize_bundle_cadence(observations)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["frame_rate_equality_required"])
        self.assertEqual(
            result["per_channel"]["rgb"]["observed_grids"],
            [(720, 1280)],
        )
        self.assertEqual(
            result["per_channel"]["native_depth"]["observed_grids"],
            [(576, 640)],
        )
        self.assertGreater(
            result["per_channel"]["rgb"]["estimated_hz"],
            result["per_channel"]["native_depth"]["estimated_hz"],
        )

    def test_cadence_rejects_a_stale_required_channel(self) -> None:
        observations = [
            self._cadence_observation(
                0,
                rgb_timestamp_us=1_000_000 + index * 33_333,
                depth_timestamp_us=1_010_000,
            )
            for index in range(3)
        ]
        result = summarize_bundle_cadence(observations)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("NATIVE_DEPTH_DID_NOT_ADVANCE", result["blockers"])
        self.assertIn("ALIGNED_DEPTH_DID_NOT_ADVANCE", result["blockers"])

    def test_live_payload_copy_reuses_preopened_mapping(self) -> None:
        mapping_name = r"Local\RgbdEvidenceTest"

        def reference(label: str) -> dict:
            return {
                "mapping_name": mapping_name,
                "label": label,
            }

        bundle = {
            "rgb": reference("rgb"),
            "depth": reference("depth"),
            "depth_aligned_to_rgb": reference("aligned"),
        }

        class Fabric:
            async def latest(self, topic: str) -> dict:
                self.topic = topic
                return {
                    "provider_id": "camera.test",
                    "data": bundle,
                }

        class Reader:
            def __init__(self) -> None:
                self.mapping_name = mapping_name
                self.read_labels: list[str] = []

            def read_ref(self, value: dict) -> bytes:
                self.read_labels.append(value["label"])
                return value["label"].encode("ascii")

        fabric = Fabric()
        reader = Reader()
        capture = object.__new__(RgbdEvidenceCapture)
        capture.fabric = fabric
        result = asyncio.run(
            capture._read_latest_payloads(
                provider_id="camera.test",
                reader=reader,
            )
        )

        self.assertEqual(fabric.topic, "camera.rgbd.bundle")
        self.assertEqual(reader.read_labels, ["aligned", "rgb"])
        self.assertEqual(result[-3:], (b"rgb", b"aligned", "FABRIC_BUFFER_REFS"))

    def test_expired_fabric_refs_use_timestamp_checked_provider_fallback(
        self,
    ) -> None:
        mapping_name = r"Local\RgbdFallbackTest"

        def reference(label: str, timestamp_us: int) -> dict:
            return {
                "mapping_name": mapping_name,
                "label": label,
                "global_timestamp_us": timestamp_us,
            }

        stale_bundle = {
            "rgb": reference("stale_rgb", 900_000),
            "depth": reference("stale_depth", 900_000),
            "depth_aligned_to_rgb": reference("stale_aligned", 900_000),
            "max_delta_us": 50_000,
        }

        class Fabric:
            async def latest(self, _topic: str) -> dict:
                return {
                    "provider_id": "camera.test",
                    "data": stale_bundle,
                }

        class Reader:
            def __init__(self) -> None:
                self.mapping_name = mapping_name

            def read_ref(self, value: dict) -> bytes:
                if value["label"].startswith("stale"):
                    raise RuntimeError(
                        "BufferRef has expired or the slot was recycled"
                    )
                return value["label"].encode("ascii")

            def latest_ref(self, stream_kind: int) -> dict:
                labels = {
                    0: "fresh_rgb",
                    1: "fresh_depth",
                    8: "fresh_aligned",
                }
                timestamps = {
                    0: 1_000_000,
                    1: 1_010_000,
                    8: 1_000_000,
                }
                return reference(labels[stream_kind], timestamps[stream_kind])

        capture = object.__new__(RgbdEvidenceCapture)
        capture.fabric = Fabric()
        result = asyncio.run(
            capture._read_latest_payloads(
                provider_id="camera.test",
                reader=Reader(),
            )
        )

        self.assertEqual(
            result[-1],
            "PROVIDER_SHARED_MEMORY_LATEST_REF_FALLBACK",
        )
        self.assertEqual(result[-3:-1], (b"fresh_rgb", b"fresh_aligned"))
        self.assertTrue(result[1]["synchronized"])
        self.assertEqual(result[1]["timestamp_delta_us"], 10_000)


if __name__ == "__main__":
    unittest.main()
