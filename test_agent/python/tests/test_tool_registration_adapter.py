from __future__ import annotations

import unittest
from types import SimpleNamespace
import time

import numpy as np

from physical_agent_test.spatial_registration_adapter import (
    CAMERA_CAPABILITIES,
    SpatialRegistrationSkillAdapter,
)
from physical_agent_test.tool_registration_adapter import (
    ARM_TRANSFORM_CAPABILITY,
    ToolControlFrameSkillAdapter,
    parse_tool_landmark_result,
)
from physical_agent_test.vlm_router import VlmInferenceResult


CAMERA_PROVIDER = "camera.test"
CAMERA_INSTANCE = "camera-instance"
CAMERA_BOOT = "camera-boot"
ARM_PROVIDER = "robot_arm.rebot_dm"
ARM_INSTANCE = "arm-instance"
ARM_BOOT = "arm-boot"


def _binding(
    capability_provider: str,
    instance_id: str,
    boot_id: str,
    capabilities: tuple[str, ...],
    *,
    binding_id: str,
    validity: str = "CURRENT",
) -> dict:
    return {
        "binding_id": binding_id,
        "status": "RESOLVED",
        "validity": validity,
        "selections": [
            {
                "capability": capability,
                "provider_id": capability_provider,
                "provider_instance_id": instance_id,
                "boot_id": boot_id,
                "available": True,
            }
            for capability in capabilities
        ],
    }


class _Manager:
    def __init__(self, *, stale_arm_after_vlm: bool = False):
        self.stale_arm_after_vlm = stale_arm_after_vlm
        self.arm_reads = 0

    async def bind_capabilities(self, capabilities, **_kwargs):
        if capabilities == list(CAMERA_CAPABILITIES):
            return {"binding_id": "camera-binding"}
        if capabilities == [ARM_TRANSFORM_CAPABILITY]:
            return {"binding_id": "arm-binding"}
        raise AssertionError(f"unexpected capabilities: {capabilities}")

    async def capability_binding(self, binding_id):
        if binding_id == "camera-binding":
            return _binding(
                CAMERA_PROVIDER,
                CAMERA_INSTANCE,
                CAMERA_BOOT,
                CAMERA_CAPABILITIES,
                binding_id=binding_id,
            )
        if binding_id == "arm-binding":
            self.arm_reads += 1
            return _binding(
                ARM_PROVIDER,
                ARM_INSTANCE,
                ARM_BOOT,
                (ARM_TRANSFORM_CAPABILITY,),
                binding_id=binding_id,
                validity=(
                    "STALE_PROVIDER_RESTARTED"
                    if self.stale_arm_after_vlm and self.arm_reads > 1
                    else "CURRENT"
                ),
            )
        raise AssertionError(f"unexpected binding: {binding_id}")


class _Capture:
    async def capture(self):
        now_us = time.time_ns() // 1000
        identity = {
            "provider_id": CAMERA_PROVIDER,
            "provider_instance_id": CAMERA_INSTANCE,
            "boot_id": CAMERA_BOOT,
            "observed_at_us": now_us,
            "data": {},
        }
        bundle = {
            **identity,
            "data": {
                "coordinate_conventions": {
                    "rgb": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                    "aligned_depth": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                }
            },
        }
        return SimpleNamespace(
            rgb=np.full((8, 10, 3), 127, dtype=np.uint8),
            depth_m=np.full((8, 10), 0.5, dtype=np.float32),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 4.0},
            timestamp_us=now_us,
            frame_number=9,
            camera_frame="camera_color",
            session_epoch="vio-epoch",
            world_frame="world",
            calibration_revision="calibration-1",
            observations={
                "bundle": bundle,
                "calibration": {
                    **identity,
                    "data": {
                        "calibration_revision": "calibration-1",
                    },
                },
                "body_pose": {
                    "provider_id": "localization.test",
                    "observed_at_us": now_us,
                    "data": {
                        "session_epoch": "vio-epoch",
                        "world_frame": "world",
                        "convention_id": (
                            "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                        ),
                    },
                },
                "vio_status": {
                    "provider_id": "localization.test",
                    "observed_at_us": now_us,
                    "data": {
                        "tracking_state": "TRACKING",
                        "session_epoch": "vio-epoch",
                        "convention_id": (
                            "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                        ),
                    },
                },
                "capture": {"copy_attempt": 1},
            },
        )


class _Fabric:
    def __init__(self):
        self.transform_requests: list[dict] = []
        registered_region = {
            "x": 0,
            "y": 0,
            "width": 10,
            "height": 8,
        }

        def channel(
            channel_id: str,
            width: int,
            height: int,
            stride_bytes: int,
            format_name: str,
            valid_region: dict | None = None,
        ) -> dict:
            return {
                "available": True,
                "channel_id": channel_id,
                "native_grid": {
                    "width": width,
                    "height": height,
                    "stride_bytes": stride_bytes,
                    "independent_resolution": True,
                    "independent_aspect_ratio": True,
                },
                "valid_region": valid_region
                or {"x": 0, "y": 0, "width": width, "height": height},
                "timestamp": {
                    "selection_order": [
                        "global_timestamp_us",
                        "system_timestamp_us",
                        "device_timestamp_us",
                    ]
                },
                "sample": {"format_name": format_name},
                "coordinate_convention_id": (
                    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                ),
                "calibration": {
                    "stream": "camera.calibration",
                    "document_path": f"{channel_id}.intrinsic",
                },
            }

        self.route = {
            "route_id": "generic",
            "capability": "camera.rgbd.route.generic_shared_memory",
            "provider_id": CAMERA_PROVIDER,
            "provider_instance_id": CAMERA_INSTANCE,
            "boot_id": CAMERA_BOOT,
            "available": True,
            "hardware_specific": False,
            "selection": {"role": "PRIMARY"},
            "transport": {
                "kind": "WINDOWS_NAMED_SHARED_MEMORY",
                "mapping_name": "Local\\ToolRegistrationTest",
                "consumer_library": "test",
                "buffer_reference_schema": "physical_agent.buffer_ref",
                "payload_location": "SHARED_MEMORY_ONLY",
                "large_payloads_on_fabric": False,
            },
            "payload_policy": {
                "large_payload_transport": "SHARED_MEMORY",
                "large_payloads_on_fabric": False,
            },
            "channel_model": {
                "same_resolution_not_required": True,
                "same_aspect_ratio_not_required": True,
                "same_boundary_not_required": True,
                "per_channel_intrinsics_required_when_used_for_3d": True,
            },
            "products": {
                "calibration": {
                    "stream": "camera.calibration",
                    "revision": "calibration-1",
                },
                "channels": {
                    "rgb": channel("rgb", 10, 8, 30, "RGB"),
                    "infrared": channel("infrared", 6, 4, 12, "Y16"),
                    "depth": channel("depth", 6, 4, 12, "Y16"),
                    "depth_registered_to_rgb": channel(
                        "depth_registered_to_rgb",
                        10,
                        8,
                        20,
                        "Y16",
                        registered_region,
                    ),
                }
            },
            "synchronization": {
                "bundle_stream": "camera.rgbd.bundle",
                "policy": "TIMESTAMP_DELTA_WITH_PROVIDER_DECLARED_THRESHOLD",
            },
            "alignments": [
                {
                    "alignment_id": "depth_to_rgb.tool_test.v1",
                    "calibration_revision": "calibration-1",
                    "output_channel": "depth_registered_to_rgb",
                    "source_channel": "depth",
                    "target_channel": "rgb",
                    "output_grid": "TARGET_CHANNEL_NATIVE_GRID",
                    "producer": "PROVIDER_CUSTOM",
                    "allows_source_target_resolution_mismatch": True,
                    "allows_source_target_aspect_ratio_mismatch": True,
                    "allows_non_overlapping_boundaries": True,
                    "boundary_model": {
                        "output_valid_region": dict(registered_region),
                    },
                }
            ],
        }

    async def latest_optional(self, stream):
        if stream != "camera.rgbd.data_routes":
            return None
        return {
            "provider_id": CAMERA_PROVIDER,
            "provider_instance_id": CAMERA_INSTANCE,
            "boot_id": CAMERA_BOOT,
            "observed_at_us": time.time_ns() // 1000,
            "data": {"routes": [self.route]},
        }

    async def transform(
        self,
        *,
        from_frame,
        to_frame,
        at_us,
        max_extrapolation_us,
        session_epoch=None,
    ):
        self.transform_requests.append(
            {
                "from_frame": from_frame,
                "to_frame": to_frame,
                "session_epoch": session_epoch,
            }
        )
        if from_frame == "camera_color":
            provider_id = "localization.local_vio"
            instance_id = "vio-instance"
            step_epoch = "vio-epoch"
        elif from_frame == "rebot_arm_tool":
            provider_id = ARM_PROVIDER
            instance_id = ARM_INSTANCE
            step_epoch = ARM_BOOT
        else:
            provider_id = "stationary.alignment"
            instance_id = "alignment-instance"
            step_epoch = "vio-epoch"
        return {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "at_us": at_us,
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": [
                {
                    "from_frame": from_frame,
                    "to_frame": to_frame,
                    "child_frame": from_frame,
                    "provider_id": provider_id,
                    "provider_instance_id": instance_id,
                    "session_epoch": step_epoch,
                    "extrapolated_by_us": min(1, max_extrapolation_us),
                }
            ],
        }


class _Router:
    async def generate(self, *, image_bytes, mime_type, prompt):
        assert image_bytes
        assert mime_type == "image/jpeg"
        assert "actual pixel" in prompt
        return VlmInferenceResult(
            text=(
                '{"scene_suitable":true,"reason":"visible",'
                '"landmarks":['
                '{"role":"acting_point","pixel_yx":[4,3],'
                '"confidence":0.95,"depth_policy":"CLOSEST_TO_CAMERA"},'
                '{"role":"axis_reference","pixel_yx":[4,7],'
                '"confidence":0.94,"depth_policy":"ROBUST_MEDIAN"},'
                '{"role":"plane_reference","pixel_yx":[2,3],'
                '"confidence":0.93,"depth_policy":"ROBUST_MEDIAN"}]}'
            ),
            backend_id="vlm.test",
            model_id="test-model",
            attempt_count=1,
            failed_attempts=(),
            quality_control_mode="OFF_FUTURE",
            elapsed_ms=1.0,
            input_sha256="hash",
            input_bytes=len(image_bytes),
            mime_type=mime_type,
        )


class ToolRegistrationAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(
        self,
        *,
        binding_mode: str = "ENFORCED",
        stale_arm_after_vlm: bool = False,
    ):
        manager = _Manager(stale_arm_after_vlm=stale_arm_after_vlm)
        fabric = _Fabric()
        spatial = SpatialRegistrationSkillAdapter(
            _Capture(),
            fabric,
            manager=manager,
            fallback_camera_provider_id=CAMERA_PROVIDER,
            binding_mode=binding_mode,
        )
        tool = ToolControlFrameSkillAdapter(
            spatial,
            _Router(),  # type: ignore[arg-type]
            manager=manager,
            fallback_arm_provider_id=ARM_PROVIDER,
            arm_base_frame="rebot_arm_base",
            arm_tool_frame="rebot_arm_tool",
            binding_mode=binding_mode,
        )
        return tool, fabric

    async def test_review_candidate_uses_separate_robot_and_vio_epochs(
        self,
    ) -> None:
        adapter, fabric = self._adapter()

        result = await adapter.run(
            tool_description="soft test tool",
            control_frame_purpose="acting point at visible tip",
            target_frame="stationary_world",
        )

        self.assertFalse(result["physical_action_submitted"])
        self.assertFalse(result["control_frame_published"])
        self.assertFalse(result["motion_usable"])
        self.assertEqual(result["vlm_provenance"]["model"], "test-model")
        by_source = {
            request["from_frame"]: request
            for request in fabric.transform_requests
        }
        self.assertIsNone(by_source["rebot_arm_tool"]["session_epoch"])
        self.assertEqual(
            by_source["rebot_arm_base"]["session_epoch"],
            "vio-epoch",
        )

    async def test_enforced_arm_binding_rejects_restart_after_vlm(self) -> None:
        adapter, _fabric = self._adapter(stale_arm_after_vlm=True)

        with self.assertRaisesRegex(RuntimeError, "tool binding enforcement"):
            await adapter.run(
                tool_description="test tool",
                control_frame_purpose="tip frame",
                target_frame="stationary_world",
            )

    def test_parser_rejects_duplicate_landmark_roles(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            parse_tool_landmark_result(
                '{"scene_suitable":true,"landmarks":['
                '{"role":"acting_point","pixel_yx":[1,2],"confidence":0.9},'
                '{"role":"acting_point","pixel_yx":[2,3],"confidence":0.9},'
                '{"role":"plane_reference","pixel_yx":[3,4],"confidence":0.9}]}'
            )


if __name__ == "__main__":
    unittest.main()
