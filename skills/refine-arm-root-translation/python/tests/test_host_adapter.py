from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest

from jsonschema import validate
import numpy as np

from refine_arm_root_translation.host_adapter import (
    ArmRootTranslationRefinementAdapter,
)


class _Manager:
    async def providers(self):
        return [
            {
                "config": {"id": "robot_arm.example"},
                "process_state": "running",
                "report": {
                    "provider_id": "robot_arm.example",
                    "residency": "HOT",
                    "health": "HEALTHY",
                    "ready": True,
                    "expired": False,
                    "instance_id": "arm-instance-1",
                    "boot_id": "arm-boot-1",
                    "last_seen": "2026-08-07T00:00:00Z",
                },
            }
        ]

    async def workcell_calibrations(self):
        return {
            "activations": [
                {
                    "activation_id": "alignment-1",
                    "state": "ACTIVE",
                    "enforcement": "ENFORCED",
                    "motion_usable": True,
                    "world_frame": "workcell/example",
                    "session_epoch": "world-epoch-1",
                    "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
                    "camera_provider_id": "camera.example",
                    "camera_provider_instance_id": "camera-instance-1",
                    "camera_boot_id": "camera-boot-1",
                    "camera_calibration_revision": "camera-calibration-1",
                }
            ]
        }

    async def refine_workcell_calibration_translation(self, request):
        return dict(request)


class _Fabric:
    def __init__(self) -> None:
        self.transform_calls: list[dict] = []

    async def latest_optional(self, stream: str):
        if stream == "robot_arm.model":
            return {
                "provider_id": "robot_arm.example",
                "provider_instance_id": "arm-instance-1",
                "boot_id": "arm-boot-1",
                "data": {
                    "model_id": "example_arm",
                    "model_revision": "example-arm-revision-1",
                },
            }
        if stream == "robot_arm.joint_state":
            return {
                "provider_id": "robot_arm.example",
                "provider_instance_id": "arm-instance-1",
                "boot_id": "arm-boot-1",
                "observed_at_us": time.time_ns() // 1000,
                "data": {
                    "velocities_rad_s": [0.0, 0.01, -0.01],
                    "feedback_age_ms": 2.0,
                    "feedback_timing": {
                        "timestamp_semantics": "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE",
                        "freshness_verified": True,
                        "timestamp_uncertainty_us": 50,
                    },
                },
            }
        if stream == "localization.vio.status":
            return {"data": {"tracking_state": "TRACKING"}}
        return None

    async def transform(self, **kwargs):
        self.transform_calls.append(dict(kwargs))
        is_camera = str(kwargs["from_frame"]).startswith("camera")
        return {
            "from_frame": kwargs["from_frame"],
            "to_frame": kwargs["to_frame"],
            "at_us": kwargs["at_us"],
            "translation_m": [0.0, 0.0, 0.0] if is_camera else [0.1, 0.2, 0.3],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": [
                {
                    "from_frame": kwargs["from_frame"],
                    "to_frame": kwargs["to_frame"],
                    "observed_at_us": kwargs["at_us"],
                    "interpolated": True,
                    "extrapolated_by_us": 0,
                }
            ],
        }


class _Spatial:
    def __init__(self) -> None:
        timestamp_us = time.time_ns() // 1000
        self.frame = SimpleNamespace(
            rgb=np.zeros((4, 6, 3), dtype=np.uint8),
            depth_m=np.ones((4, 6), dtype=np.float32),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 3.0, "cy": 2.0},
            timestamp_us=timestamp_us,
            frame_number=7,
            camera_frame="camera_color_optical",
            session_epoch="world-epoch-1",
            observations={
                "vio_status": {"data": {"tracking_state": "TRACKING"}},
                "bundle": {
                    "data": {
                        "synchronized": True,
                        "max_delta_us": 10_000,
                        "rgb": {"global_timestamp_us": timestamp_us},
                        "depth_aligned_to_rgb": {
                            "global_timestamp_us": timestamp_us + 1_000
                        },
                    }
                },
            },
        )

    async def prepare_context(self, **_kwargs):
        return SimpleNamespace(
            frame=self.frame,
            target_from_camera=np.eye(4, dtype=np.float64),
        )

    async def revalidate_context_binding(self, _context):
        return None


class _Vlm:
    async def generate_images(self, *, images, prompt):
        return SimpleNamespace(
            text="{}",
            as_dict=lambda: {"image_count": len(images), "prompt": prompt},
        )


class _Evidence:
    async def register_channels(self, **kwargs):
        return {
            "schema": "midbrain.visual_evidence",
            "schema_version": 2,
            "evidence_id": "evidence-1",
            "title": kwargs["title"],
            "default_channel": kwargs["default_channel"],
        }


class _RpcResponseSink:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        return None


class _EndToEndManager:
    def __init__(self) -> None:
        self.record = {
            "activation_id": "alignment-1",
            "state": "ACTIVE",
            "enforcement": "ENFORCED",
            "motion_usable": True,
            "translation_refinement_revision": 0,
            "world_frame": "workcell/example",
            "arm_base_frame": "rebot_arm_base",
            "session_epoch": "world-epoch-1",
            "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "camera_provider_id": "camera.example",
            "camera_provider_instance_id": "camera-instance-1",
            "camera_boot_id": "camera-boot-1",
            "camera_calibration_revision": "camera-calibration-1",
            "transforms": {
                "world_from_base": {
                    "translation_m": [0.0, 0.0, 0.0],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                }
            },
            "last_translation_refinement": None,
        }
        self.requests: list[dict] = []

    async def providers(self):
        return [
            {
                "config": {"id": "robot_arm.test"},
                "process_state": "running",
                "report": {
                    "provider_id": "robot_arm.test",
                    "residency": "HOT",
                    "health": "HEALTHY",
                    "ready": True,
                    "expired": False,
                    "instance_id": "arm-instance-1",
                    "boot_id": "arm-boot-1",
                    "last_seen": "2026-08-07T00:00:00Z",
                },
            }
        ]

    async def workcell_calibrations(self):
        return {"activations": [json.loads(json.dumps(self.record))]}

    async def refine_workcell_calibration_translation(self, request):
        self.requests.append(json.loads(json.dumps(request)))
        self.record["translation_refinement_revision"] += 1
        self.record["transforms"]["world_from_base"] = json.loads(
            json.dumps(request["proposed_world_from_base"])
        )
        return json.loads(json.dumps(self.record))


class _EndToEndFabric(_Fabric):
    async def latest_optional(self, stream: str):
        if stream == "robot_arm.model":
            return {
                "provider_id": "robot_arm.test",
                "provider_instance_id": "arm-instance-1",
                "boot_id": "arm-boot-1",
                "data": {
                    "model_id": "rebot_arm_b601_dm",
                    "model_revision": (
                        "rebot-official-fixed-end-0.1.21-pos-speed-motor-envelope"
                    ),
                },
            }
        if stream == "robot_arm.joint_state":
            observation = await super().latest_optional(stream)
            return {
                **observation,
                "provider_id": "robot_arm.test",
            }
        return await super().latest_optional(stream)

    async def transform(self, **kwargs):
        is_camera = str(kwargs["from_frame"]).startswith("camera")
        return {
            "from_frame": kwargs["from_frame"],
            "to_frame": kwargs["to_frame"],
            "at_us": kwargs["at_us"],
            "translation_m": [0.0, 0.0, 0.0] if is_camera else [0.08, 0.0, 0.999],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": [
                {
                    "from_frame": kwargs["from_frame"],
                    "to_frame": kwargs["to_frame"],
                    "observed_at_us": kwargs["at_us"],
                    "interpolated": True,
                    "extrapolated_by_us": 0,
                }
            ],
        }


class _EndToEndSpatial(_Spatial):
    def __init__(self) -> None:
        super().__init__()
        self.frame = SimpleNamespace(
            **{
                **vars(self.frame),
                "rgb": np.full((11, 11, 3), 80, dtype=np.uint8),
                "depth_m": np.ones((11, 11), dtype=np.float32),
                "intrinsics": {
                    "fx": 11.0,
                    "fy": 11.0,
                    "cx": 5.0,
                    "cy": 5.0,
                },
            }
        )


class _DetectionVlm:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_images(self, *, images, prompt):
        self.calls += 1
        response = {
            "schema": "midbrain.effector_landmark_detection",
            "schema_version": 2,
            "scene_suitable": True,
            "landmark_id": "rail_lateral_endpoint_mean",
            "coordinate_space": "NORMALIZED_YX_0_1000_PER_IMAGE",
            "reason": "Both neon-green rail endpoints are visible in RGB and depth.",
            "points": [
                {
                    "point_id": "rail_lateral_left",
                    "rgb_yx_0_1000": [500, 400],
                    "registered_depth_yx_0_1000": [500, 400],
                    "confidence": 0.96,
                    "same_surface_confidence": 0.95,
                    "reason": "The selected depth pixel is on the left rail face.",
                },
                {
                    "point_id": "rail_lateral_right",
                    "rgb_yx_0_1000": [500, 600],
                    "registered_depth_yx_0_1000": [500, 600],
                    "confidence": 0.97,
                    "same_surface_confidence": 0.96,
                    "reason": "The selected depth pixel is on the right rail face.",
                },
            ],
        }
        return SimpleNamespace(
            text=json.dumps(response),
            as_dict=lambda: {
                "model_id": "vlm.test",
                "image_count": len(images),
                "prompt_length": len(prompt),
            },
        )


class _UnavailableLocalTransformFabric(_EndToEndFabric):
    async def transform(self, **kwargs):
        if kwargs["from_frame"] == "rebot_arm_tool":
            raise RuntimeError("no local arm transform path at requested time")
        return await super().transform(**kwargs)


class _FailsAfterPreflightFabric(_EndToEndFabric):
    def __init__(self) -> None:
        super().__init__()
        self.local_transform_calls = 0

    async def transform(self, **kwargs):
        if kwargs["from_frame"] == "rebot_arm_tool":
            self.local_transform_calls += 1
            if self.local_transform_calls > 1:
                raise RuntimeError(
                    "local arm transform publication stopped after preflight"
                )
        return await super().transform(**kwargs)


class ArmRootTranslationRefinementAdapterTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.skill_root = Path(self.temporary.name) / "generic-skill"
        profiles = self.skill_root / "profiles"
        profiles.mkdir(parents=True)
        self.profile_path = profiles / "example_effector.v1.json"
        self.profile_path.write_text(
            json.dumps(
                {
                    "profile_revision": "example-effector-v1",
                    "robot_compatibility": {
                        "arm_base_frame": "example_arm_base",
                        "controlled_frame": "example_tool_point",
                    },
                    "capture_motion_policy": {
                        "maximum_landmark_motion_m": 0.005,
                        "additional_camera_timing_margin_us": 20_000,
                        "arm_feedback_age_field_path": [
                            "data",
                            "feedback_age_ms",
                        ],
                        "fallback_arm_feedback_age_ms": 20.0,
                        "maximum_arm_feedback_age_ms": 100.0,
                        "preferred_arm_feedback_observation_age_ms": 100.0,
                        "maximum_transform_wait_ms": 100.0,
                        "transform_retry_interval_ms": 1.0,
                        "temporal_sample_count": 5,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(self) -> ArmRootTranslationRefinementAdapter:
        return ArmRootTranslationRefinementAdapter(
            skill_root=self.skill_root,
            profile_path=self.profile_path,
            manager=_Manager(),
            fabric=_Fabric(),
            spatial=_Spatial(),
            vlm_router=_Vlm(),
            visual_evidence_store=_Evidence(),
        )

    def test_profile_must_be_selected_inside_skill_profiles(self) -> None:
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "profiles directory"):
            ArmRootTranslationRefinementAdapter(
                skill_root=self.skill_root,
                profile_path=outside,
                manager=_Manager(),
                fabric=_Fabric(),
                spatial=_Spatial(),
                vlm_router=_Vlm(),
                visual_evidence_store=_Evidence(),
            )

    async def test_host_rpc_preserves_manager_http_conflict(self) -> None:
        class _ConflictResponse:
            status_code = 409
            text = '{"error":"active arm identity changed"}'

            @staticmethod
            def json():
                return {"error": "active arm identity changed"}

        class _ConflictManager(_Manager):
            async def refine_workcell_calibration_translation(self, request):
                error = RuntimeError("409 Conflict")
                error.response = _ConflictResponse()
                raise error

        adapter = ArmRootTranslationRefinementAdapter(
            skill_root=self.skill_root,
            profile_path=self.profile_path,
            manager=_ConflictManager(),
            fabric=_Fabric(),
            spatial=_Spatial(),
            vlm_router=_Vlm(),
            visual_evidence_store=_Evidence(),
        )
        sink = _RpcResponseSink()
        process = SimpleNamespace(stdin=sink)

        await adapter._answer_request(
            process,
            {
                "id": 17,
                "method": "manager.refine_workcell_translation",
                "parameters": {"request": {}},
            },
        )

        response = json.loads(sink.payload.decode("utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["status_code"], 409)
        self.assertEqual(
            response["error"]["response_body"]["error"],
            "active arm identity changed",
        )

    async def test_capture_uses_timestamped_fk_motion_window(self) -> None:
        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as session:
            adapter._session_dir = Path(session)
            result = await adapter._capture(
                {
                    "world_frame": "workcell/example",
                    "arm_base_frame": "example_arm_base",
                    "controlled_frame": "example_tool_point",
                }
            )

        self.assertEqual(result["tracking_state"], "TRACKING")
        self.assertEqual(result["provenance"]["arm_base_frame"], "example_arm_base")
        self.assertEqual(np.asarray(result["base_from_tool"]).shape, (4, 4))
        temporal = result["temporal_alignment"]
        self.assertEqual(
            temporal["policy_id"],
            "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1",
        )
        self.assertEqual(len(temporal["base_from_tool_samples"]), 5)
        self.assertTrue(
            all(
                sample["maximum_extrapolation_us"] == 0
                for sample in temporal["base_from_tool_samples"]
            )
        )
        self.assertEqual(temporal["arm_feedback_age_us"], 2_000)
        self.assertEqual(
            temporal["world_from_camera_temporal_provenance"]["at_us"],
            temporal["registered_depth_timestamp_us"],
        )
        self.assertEqual(
            temporal["fk_reference_timestamp_us"],
            temporal["registered_depth_timestamp_us"] + 2_000,
        )

    async def test_capture_rejects_fk_that_cannot_be_bracketed(self) -> None:
        class _ExtrapolatingFabric(_Fabric):
            async def transform(self, **kwargs):
                result = await super().transform(**kwargs)
                result["path"][0]["interpolated"] = False
                result["path"][0]["extrapolated_by_us"] = 2_000
                return result

        adapter = self._adapter()
        adapter.fabric = _ExtrapolatingFabric()
        adapter.profile["capture_motion_policy"][
            "maximum_transform_wait_ms"
        ] = 5.0
        adapter.profile["capture_motion_policy"][
            "transform_retry_interval_ms"
        ] = 1.0
        with tempfile.TemporaryDirectory() as session:
            adapter._session_dir = Path(session)
            with self.assertRaisesRegex(RuntimeError, "could not bracket"):
                await adapter._capture(
                    {
                        "world_frame": "workcell/example",
                        "arm_base_frame": "example_arm_base",
                        "controlled_frame": "example_tool_point",
                    }
                )

    def test_stale_joint_observation_uses_conservative_profile_maximum(
        self,
    ) -> None:
        adapter = self._adapter()
        timing = adapter._arm_feedback_age(
            {
                "observed_at_us": time.time_ns() // 1000 - 500_000,
                "data": {"feedback_age_ms": 2.0},
            }
        )

        self.assertEqual(timing["age_us"], 100_000)
        self.assertEqual(
            timing["source"],
            "PROFILE_CONSERVATIVE_MAXIMUM_STALE_JOINT_STATE",
        )
        self.assertFalse(timing["fresh_for_feedback_age"])
        self.assertGreater(timing["observation_age_us"], 300_000)

    def test_measured_acquisition_timestamp_does_not_double_count_feedback_age(
        self,
    ) -> None:
        adapter = self._adapter()
        adapter.profile["capture_motion_policy"][
            "arm_transform_timestamp_semantics"
        ] = "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE"
        timing = adapter._arm_feedback_age(
            {
                "observed_at_us": time.time_ns() // 1000,
                "data": {
                    "feedback_age_ms": 12.0,
                    "feedback_timing": {
                        "timestamp_semantics": "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE",
                        "freshness_verified": True,
                        "timestamp_uncertainty_us": 75,
                    },
                },
            }
        )

        self.assertEqual(timing["age_us"], 0)
        self.assertEqual(
            timing["source"],
            "JOINT_STATE_TIMESTAMP_IS_MEASURED_ACQUISITION",
        )
        self.assertEqual(timing["timestamp_uncertainty_us"], 75)

    def test_measured_acquisition_profile_rejects_legacy_joint_timestamps(
        self,
    ) -> None:
        adapter = self._adapter()
        adapter.profile["capture_motion_policy"][
            "arm_transform_timestamp_semantics"
        ] = "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE"

        with self.assertRaisesRegex(RuntimeError, "timing metadata"):
            adapter._arm_feedback_age(
                {
                    "observed_at_us": time.time_ns() // 1000,
                    "data": {"feedback_age_ms": 2.0},
                }
            )

    async def test_arm_identity_is_provider_and_model_driven(self) -> None:
        result = await self._adapter()._arm_identity({})

        self.assertEqual(result["arm_provider_id"], "robot_arm.example")
        self.assertEqual(result["arm_model_id"], "example_arm")
        self.assertEqual(result["arm_model_revision"], "example-arm-revision-1")

    def test_production_adapter_contains_no_arm_specific_identifier(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "refine_arm_root_translation"
            / "host_adapter.py"
        ).read_text(encoding="utf-8").lower()

        self.assertNotIn("rebot", source)
        self.assertNotIn("b601", source)

    async def test_private_venv_rpc_applies_one_small_translation_update(
        self,
    ) -> None:
        workspace = Path(__file__).resolve().parents[4]
        skill_root = workspace / "skills" / "refine-arm-root-translation"
        skill_python = skill_root / ".venv" / "Scripts" / "python.exe"
        if not skill_python.is_file():
            self.skipTest("translation-refinement private venv is not installed")
        manager = _EndToEndManager()
        vlm = _DetectionVlm()
        adapter = ArmRootTranslationRefinementAdapter(
            skill_root=skill_root,
            profile_path=(
                skill_root / "profiles" / "rebot_b601_dm_gripper.v3.json"
            ),
            manager=manager,
            fabric=_EndToEndFabric(),
            spatial=_EndToEndSpatial(),
            vlm_router=vlm,
            visual_evidence_store=_Evidence(),
        )

        result = await adapter.run(sample_count=3)

        self.assertEqual(result["status"], "TRANSLATION_UPDATE_READY")
        self.assertTrue(result["state_update_applied"])
        self.assertEqual(result["active_revision"], 1)
        self.assertEqual(vlm.calls, 3)
        self.assertEqual(len(manager.requests), 1)
        self.assertEqual(
            result["multi_sample_refinement"]["requested_sample_count"],
            3,
        )
        proposed = manager.requests[0]["proposed_world_from_base"]
        self.assertTrue(
            np.allclose(proposed["translation_m"], [0.0, 0.0, 0.001])
        )
        self.assertEqual(proposed["rotation_xyzw"], [0.0, 0.0, 0.0, 1.0])

    async def test_missing_arm_transform_returns_recoverable_dependency(
        self,
    ) -> None:
        workspace = Path(__file__).resolve().parents[4]
        skill_root = workspace / "skills" / "refine-arm-root-translation"
        skill_python = skill_root / ".venv" / "Scripts" / "python.exe"
        if not skill_python.is_file():
            self.skipTest("translation-refinement private venv is not installed")
        adapter = ArmRootTranslationRefinementAdapter(
            skill_root=skill_root,
            profile_path=(
                skill_root / "profiles" / "rebot_b601_dm_gripper.v3.json"
            ),
            manager=_EndToEndManager(),
            fabric=_UnavailableLocalTransformFabric(),
            spatial=_EndToEndSpatial(),
            vlm_router=_DetectionVlm(),
            visual_evidence_store=_Evidence(),
        )
        adapter.profile["capture_motion_policy"][
            "maximum_transform_wait_ms"
        ] = 5.0

        result = await adapter.run(sample_count=5)

        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(result["workflow_complete"])
        self.assertFalse(result["state_update_applied"])
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": "robot_arm.test",
                    "action": "hot",
                    "required_capability": "robot_arm.joint_state",
                },
            },
        )
        self.assertEqual(
            result["retry_after_prerequisite"]["arguments"],
            {
                "adoption_factor": 1.0,
                "sample_count": 5,
                "landmark_id": None,
            },
        )
        self.assertFalse(result["physical_motion_submitted"])
        schema = json.loads(
            (
                skill_root
                / "schemas"
                / "arm_root_translation_refinement.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        validate(instance=result, schema=schema)

    async def test_capture_time_arm_transform_loss_is_also_recoverable(
        self,
    ) -> None:
        workspace = Path(__file__).resolve().parents[4]
        skill_root = workspace / "skills" / "refine-arm-root-translation"
        skill_python = skill_root / ".venv" / "Scripts" / "python.exe"
        if not skill_python.is_file():
            self.skipTest("translation-refinement private venv is not installed")
        fabric = _FailsAfterPreflightFabric()
        vlm = _DetectionVlm()
        adapter = ArmRootTranslationRefinementAdapter(
            skill_root=skill_root,
            profile_path=(
                skill_root / "profiles" / "rebot_b601_dm_gripper.v3.json"
            ),
            manager=_EndToEndManager(),
            fabric=fabric,
            spatial=_EndToEndSpatial(),
            vlm_router=vlm,
            visual_evidence_store=_Evidence(),
        )
        adapter.profile["capture_motion_policy"][
            "maximum_transform_wait_ms"
        ] = 5.0

        result = await adapter.run(sample_count=3)

        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertIn("stopped after preflight", result["reason"])
        self.assertEqual(result["dependency"]["kind"], "LOCAL_ARM_FK_STREAM")
        self.assertEqual(vlm.calls, 0)
        self.assertGreater(fabric.local_transform_calls, 1)


if __name__ == "__main__":
    unittest.main()
