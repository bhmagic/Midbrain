from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import numpy as np

from physical_agent_test.effector_front_adapter import (
    EffectorFrontSkillAdapter,
    build_effector_front_evidence,
)
from physical_agent_test.vlm_router import VlmInferenceResult


class _Snapshot:
    def __init__(self, identity: str):
        self.identity = identity

    def as_dict(self):
        return {"identity": self.identity}


class _Spatial:
    binding_mode = "ENFORCED"
    generic_route_mode = "ENFORCED"

    def __init__(self, *, age_us: int = 0, revalidation_error: str | None = None):
        self.revalidation_error = revalidation_error
        timestamp_us = time.time_ns() // 1000 - age_us
        frame = SimpleNamespace(
            rgb=np.full((8, 12, 3), 120, dtype=np.uint8),
            depth_m=np.full((4, 6), 0.7, dtype=np.float32),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 2.5, "cy": 1.5},
            timestamp_us=timestamp_us,
            frame_number=9,
            camera_frame="camera_registered_depth",
            session_epoch="vio-epoch",
            world_frame="stationary_world",
            calibration_revision="calibration-1",
        )
        self.context = SimpleNamespace(
            frame=frame,
            valid_region={"x": 0, "y": 0, "width": 6, "height": 4},
            binding=_Snapshot("binding-at-capture"),
            selection=_Snapshot("generic-route"),
            target_from_camera=np.eye(4),
            temporal_evidence={"bundle": {"accepted": True}},
        )

    async def prepare_context(self, *, target_frame, skill_id):
        self.context.skill_id = skill_id
        self.context.target_frame = target_frame
        return self.context

    async def revalidate_context_binding(self, _context):
        if self.revalidation_error:
            raise RuntimeError(self.revalidation_error)
        return _Snapshot("binding-after-vlm")

    def capture_provenance(self, _context):
        return {"frame_number": 9}

    def transform_provenance(self, _context):
        return {"at_us": self.context.frame.timestamp_us}

    def route_metadata(self, _context):
        return {"valid_region": self.context.valid_region}


class _Router:
    async def generate(self, *, image_bytes, mime_type, prompt):
        if not image_bytes.startswith(b"\x89PNG"):
            raise AssertionError("effector-front evidence must be lossless PNG")
        if mime_type != "image/png":
            raise AssertionError("effector-front evidence MIME type is wrong")
        if "does NOT mean closest to the camera" not in prompt:
            raise AssertionError("front-direction semantics are missing")
        if "ORIGINAL registered-depth grid" not in prompt:
            raise AssertionError("depth-grid coordinate contract is missing")
        return VlmInferenceResult(
            text=(
                '{"schema":"physical_agent.effector_front_landmark_vlm",'
                '"schema_version":1,"scene_suitable":true,'
                '"reason":"distal rigid point with valid depth",'
                '"effector_configuration":"MOUNTED_TOOL",'
                '"front_geometry":"SINGLE_POINT",'
                '"depth_fallback_reason":"REFLECTIVE_FRONT_MISSING_DEPTH",'
                '"front_points":[{"point_id":"front",'
                '"registered_depth_pixel_yx":[2,3],"confidence":0.93,'
                '"selected_surface":"TOOL_BODY_OR_HANDLE",'
                '"selection_reason":"most distal valid point on the tool"}]}'
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


class EffectorFrontAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_evidence_uses_registered_depth_coordinate_grid(self):
        payload, metadata = build_effector_front_evidence(
            np.zeros((8, 12, 3), dtype=np.uint8),
            np.ones((4, 6), dtype=np.float32),
            valid_region={"x": 1, "y": 1, "width": 4, "height": 2},
        )

        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(metadata["rgb_source_grid"], [8, 12])
        self.assertEqual(metadata["registered_depth_grid"], [4, 6])
        self.assertTrue(metadata["rgb_resampled_to_registered_depth_grid"])
        self.assertTrue(metadata["native_depth_grid_pixels_preserved"])
        self.assertEqual(metadata["panel_display_scale"], 1.0)
        self.assertEqual(
            metadata["coordinate_contract"],
            "ORIGINAL_REGISTERED_DEPTH_PIXEL_YX",
        )

    async def test_target_frame_is_required(self):
        adapter = EffectorFrontSkillAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(ValueError, "target_frame"):
            await adapter.run(target_frame=None)  # type: ignore[arg-type]

    async def test_single_front_is_registered_without_motion(self):
        adapter = EffectorFrontSkillAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        result = await adapter.run(target_frame="stationary_world")

        self.assertTrue(result["eligible_for_control_math"])
        self.assertEqual(
            result["front_points"][0]["registered_depth_pixel_yx"],
            [2, 3],
        )
        self.assertEqual(
            result["control_reference"]["method"],
            "SINGLE_REGISTERED_3D_POINT",
        )
        self.assertFalse(result["physical_action_submitted"])
        self.assertFalse(result["control_frame_published"])
        self.assertEqual(
            result["vlm_route"]["backend_id"],
            "vlm.test",
        )

    async def test_camera_binding_is_revalidated_after_vlm(self):
        adapter = EffectorFrontSkillAdapter(
            _Spatial(revalidation_error="camera restarted after capture"),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(RuntimeError, "camera restarted"):
            await adapter.run(target_frame="stationary_world")

    async def test_source_age_is_checked_after_vlm(self):
        adapter = EffectorFrontSkillAdapter(
            _Spatial(age_us=61_000_000),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
            maximum_source_age_at_completion_ms=60_000,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "SOURCE_TOO_OLD_AFTER_VLM",
        ):
            await adapter.run(target_frame="stationary_world")


if __name__ == "__main__":
    unittest.main()
