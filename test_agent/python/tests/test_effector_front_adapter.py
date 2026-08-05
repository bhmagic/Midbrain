from __future__ import annotations

import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from physical_agent_test.effector_front_adapter import (
    EffectorFrontSkillAdapter,
    apply_controller_consistency_policy,
    build_controller_fk_effector_fallback,
    build_effector_front_evidence,
)
from physical_agent_test.vlm_router import VlmInferenceResult
from physical_agent_test.visual_evidence import VisualEvidenceStore


class _Snapshot:
    def __init__(self, identity: str):
        self.identity = identity

    def as_dict(self):
        return {"identity": self.identity}


class _Spatial:
    binding_mode = "ENFORCED"
    generic_route_mode = "ENFORCED"
    maximum_transform_extrapolation_us = 750_000

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
        self.fabric = _Fabric()

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
        if "NORMALIZED_0_1000" not in prompt:
            raise AssertionError("normalized coordinate contract is missing")
        return VlmInferenceResult(
            text=(
                '{"schema":"physical_agent.effector_front_landmark_vlm",'
                '"schema_version":2,'
                '"coordinate_space":"NORMALIZED_0_1000",'
                '"scene_suitable":true,'
                '"reason":"distal rigid point with valid depth",'
                '"effector_configuration":"MOUNTED_TOOL",'
                '"front_geometry":"SINGLE_POINT",'
                '"depth_fallback_reason":"REFLECTIVE_FRONT_MISSING_DEPTH",'
                '"front_points":[{"point_id":"front",'
                '"registered_depth_pixel_yx":[667,600],"confidence":0.93,'
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


class _Fabric:
    async def transform(self, *, to_frame, **_arguments):
        if to_frame not in {"rebot_arm_base", "camera_registered_depth"}:
            raise RuntimeError("unexpected target frame")
        return {
            "translation_m": [0.0035, 0.0035, 0.7],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        }


class _LatestOnlyFabric(_Fabric):
    async def transform(self, *, to_frame, at_us=None, **arguments):
        if at_us is not None:
            raise RuntimeError("capture-time controller transform unavailable")
        return await super().transform(to_frame=to_frame, **arguments)


class _PriorRouter(_Router):
    def __init__(self):
        self.prompt = ""

    async def generate(self, *, image_bytes, mime_type, prompt):
        self.prompt = prompt
        return await super().generate(
            image_bytes=image_bytes,
            mime_type=mime_type,
            prompt=prompt,
        )


class _NoEffectorDepthRouter(_Router):
    async def generate(self, *, image_bytes, mime_type, prompt):
        result = await super().generate(
            image_bytes=image_bytes,
            mime_type=mime_type,
            prompt=prompt,
        )
        return replace(
            result,
            text=(
                '{"schema":"physical_agent.effector_front_landmark_vlm",'
                '"schema_version":1,"scene_suitable":false,'
                '"reason":"visible jaws have invalid registered depth",'
                '"effector_configuration":"UNCERTAIN",'
                '"front_geometry":"UNKNOWN",'
                '"depth_fallback_reason":"UNCERTAIN",'
                '"front_points":[]}'
            ),
        )


class EffectorFrontAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_controller_fk_fallback_is_explicitly_degraded(self):
        result = build_controller_fk_effector_fallback(
            controller_reference={
                "source": "CURRENT_CONTROLLER_FORWARD_KINEMATICS",
                "target_point_m": [0.25, 0.0, 0.2],
            },
            vlm_result={"reason": "effector depth is invalid"},
            observed_at_us=1,
            source_frame="camera",
            target_frame="rebot_arm_base",
            calibration_revision="calibration-1",
            route_provenance={},
            maximum_arm_radius_m=1.2,
        )

        self.assertTrue(result["eligible_for_control_math"])
        self.assertFalse(result["visual_measurement_usable"])
        self.assertEqual(
            result["control_reference"]["method"],
            "CURRENT_CONTROLLER_FORWARD_KINEMATICS",
        )
        self.assertEqual(result["uncertainty_radius_m"], 0.04)

    async def test_arm_base_location_falls_back_when_effector_depth_is_invalid(self):
        adapter = EffectorFrontSkillAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _NoEffectorDepthRouter(),  # type: ignore[arg-type]
        )

        result = await adapter.run(target_frame="rebot_arm_base")

        self.assertEqual(result["status"], "CONTROLLER_FK_REFERENCE_READY")
        self.assertTrue(result["eligible_for_control_math"])
        self.assertEqual(
            result["controller_consistency"]["decision"],
            "CONTROLLER_FK_FALLBACK",
        )
    def test_controller_consistency_accepts_nearby_visual_reference(self):
        result = apply_controller_consistency_policy(
            {
                "status": "REFERENCE_READY",
                "eligible_for_control_math": True,
                "control_reference": {"target_point_m": [0.3, 0.0, 0.2]},
            },
            controller_reference={"target_point_m": [0.25, 0.0, 0.2]},
        )

        self.assertEqual(result["controller_consistency"]["decision"], "ACCEPT")
        self.assertTrue(result["eligible_for_control_math"])

    def test_controller_consistency_rejects_background_as_effector(self):
        result = apply_controller_consistency_policy(
            {
                "status": "REFERENCE_READY",
                "eligible_for_control_math": True,
                "control_reference": {
                    "target_point_m": [-2.2368, -0.4932, -0.6694]
                },
            },
            controller_reference={
                "target_point_m": [0.2582, -0.0002, 0.2130]
            },
        )

        self.assertEqual(result["status"], "CONTROLLER_CONSISTENCY_REJECTED")
        self.assertFalse(result["eligible_for_control_math"])
        self.assertFalse(result["motion_usable"])
        self.assertIn(
            "EFFECTOR_OUTSIDE_CONFIGURED_ARM_RADIUS",
            result["quality_reasons"],
        )
        self.assertIn(
            "EFFECTOR_DISAGREES_WITH_CONTROLLER_FK",
            result["quality_reasons"],
        )

    def test_controller_consistency_keeps_missing_fk_as_diagnostic(self):
        result = apply_controller_consistency_policy(
            {
                "status": "REFERENCE_READY",
                "eligible_for_control_math": True,
                "control_reference": {"target_point_m": [0.3, 0.0, 0.2]},
            },
            controller_reference=None,
        )

        self.assertEqual(result["status"], "REFERENCE_READY")
        self.assertTrue(result["eligible_for_control_math"])
        self.assertEqual(result["uncertainty_radius_m"], 0.04)
        self.assertEqual(
            result["controller_consistency"]["decision"],
            "ACCEPT_DEGRADED_NO_CONTROLLER_FK",
        )
        self.assertIn(
            "CONTROLLER_FK_REFERENCE_UNAVAILABLE",
            result["quality_reasons"],
        )

    async def test_arm_base_location_uses_controller_projection_prior(self):
        router = _PriorRouter()
        adapter = EffectorFrontSkillAdapter(
            _Spatial(),  # type: ignore[arg-type]
            router,  # type: ignore[arg-type]
        )

        result = await adapter.run(target_frame="rebot_arm_base")

        self.assertEqual(result["controller_consistency"]["decision"], "ACCEPT")
        self.assertTrue(result["eligible_for_control_math"])
        self.assertIn("Controller forward kinematics predicts", router.prompt)
        self.assertIn("[667, 600]", router.prompt)

    async def test_controller_reference_uses_fabric_latest_fallback(self):
        spatial = _Spatial()
        spatial.fabric = _LatestOnlyFabric()
        adapter = EffectorFrontSkillAdapter(
            spatial,  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
        )

        result = await adapter.run(target_frame="rebot_arm_base")

        reference = result["controller_consistency"]["controller_reference"]
        self.assertEqual(
            reference["source"],
            "LATEST_FABRIC_CONTROLLER_FORWARD_KINEMATICS",
        )
        self.assertEqual(
            reference["temporal_policy"],
            "FABRIC_BEST_AVAILABLE_LATEST_FALLBACK",
        )
        self.assertIn(
            "capture-time controller transform unavailable",
            reference["capture_time_query_error"],
        )

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
            "NORMALIZED_0_1000_YX",
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
            result["vlm_geometry"]["source_front_points"][0][
                "registered_depth_pixel_yx"
            ],
            [667, 600],
        )
        self.assertEqual(
            result["vlm_geometry"]["registered_depth_front_points"][0][
                "registered_depth_pixel_yx"
            ],
            [2, 3],
        )
        self.assertEqual(
            result["vlm_route"]["backend_id"],
            "vlm.test",
        )

    async def test_effector_visual_evidence_has_switchable_rgbd_channels(self):
        store = VisualEvidenceStore()
        adapter = EffectorFrontSkillAdapter(
            _Spatial(),  # type: ignore[arg-type]
            _Router(),  # type: ignore[arg-type]
            visual_evidence_store=store,
        )

        result = await adapter.run(target_frame="stationary_world")

        visual = result["visual_evidence"]
        self.assertEqual(visual["default_channel"], "rgb_depth")
        self.assertEqual(
            [channel["id"] for channel in visual["channels"]],
            ["rgb", "depth", "rgb_depth"],
        )
        self.assertEqual(len(visual["annotations"]), 1)
        self.assertEqual(
            set(visual["annotations"][0]["applies_to_channels"]),
            {"rgb", "depth", "rgb_depth"},
        )

    async def test_visual_evidence_image_can_be_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = EffectorFrontSkillAdapter(
                _Spatial(),  # type: ignore[arg-type]
                _Router(),  # type: ignore[arg-type]
                evidence_dir=Path(directory),
            )

            result = await adapter.run(target_frame="stationary_world")

            evidence = result["evidence_image"]
            path = Path(evidence["path"])
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(evidence["mime_type"], "image/png")

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
