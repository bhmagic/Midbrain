from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("agents")

from jsonschema import ValidationError

from physical_agent_test.agent_driver import PrototypeAgentDriver
from physical_agent_test.spatial_registration_adapter import (
    CAMERA_CAPABILITIES,
    SpatialRegistrationSkillAdapter,
)


PROVIDER_ID = "camera.femto_bolt"
INSTANCE_ID = "camera-instance-1"
BOOT_ID = "camera-boot-1"


def _identity_observation(data: dict | None = None) -> dict:
    now_us = time.time_ns() // 1000
    return {
        "provider_id": PROVIDER_ID,
        "provider_instance_id": INSTANCE_ID,
        "boot_id": BOOT_ID,
        "observed_at_us": now_us,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "clock_domain": "unix_epoch_us",
        "valid": True,
        "data": data or {},
    }


def _binding(*, validity: str = "CURRENT", boot_id: str = BOOT_ID) -> dict:
    return {
        "binding_id": "binding-1",
        "status": "RESOLVED",
        "validity": validity,
        "validation_issues": [],
        "selections": [
            {
                "capability": capability,
                "provider_id": PROVIDER_ID,
                "provider_instance_id": INSTANCE_ID,
                "boot_id": boot_id,
                "available": True,
            }
            for capability in CAMERA_CAPABILITIES
        ],
    }


def _route(*, generic: bool = True, boot_id: str = BOOT_ID) -> dict:
    route = {
        "route_id": "generic-route" if generic else "direct-route",
        "capability": (
            "camera.rgbd.route.generic_shared_memory"
            if generic
            else "camera.rgbd.route.direct_shared_memory"
        ),
        "provider_id": PROVIDER_ID,
        "provider_instance_id": INSTANCE_ID,
        "boot_id": boot_id,
        "available": True,
        "hardware_specific": not generic,
        "selection": {
            "role": "PRIMARY" if generic else "COMPATIBILITY_FALLBACK",
        },
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "consumer_library": "orbbec_femto_provider.shared_memory_access",
        },
    }
    if not generic:
        return route

    registered_region = {
        "x": 1,
        "y": 1,
        "width": 6,
        "height": 4,
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
            "calibration": {
                "stream": "camera.calibration",
                "document_path": f"{channel_id}.intrinsic",
            },
        }

    route.update(
        {
            "transport": {
                **route["transport"],
                "mapping_name": "Local\\SpatialAdapterTest",
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
                    "rgb": channel("rgb", 8, 6, 24, "RGB"),
                    "infrared": channel("infrared", 4, 3, 8, "Y16"),
                    "depth": channel("depth", 4, 3, 8, "Y16"),
                    "depth_registered_to_rgb": channel(
                        "depth_registered_to_rgb",
                        8,
                        6,
                        16,
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
                    "alignment_id": "depth_to_rgb.test.v1",
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
    )
    return route


class _Manager:
    def __init__(self, values: list[dict] | None = None):
        self.values = list(values or [_binding(), _binding()])
        self.bind_requests: list[dict] = []

    async def bind_capabilities(self, required_capabilities, **kwargs):
        self.bind_requests.append(
            {
                "required_capabilities": list(required_capabilities),
                **kwargs,
            }
        )
        return {"binding_id": "binding-1"}

    async def capability_binding(self, _binding_id):
        return self.values.pop(0)


class _Fabric:
    def __init__(
        self,
        *,
        route: dict | None = None,
        transform_epoch: str = "vio-epoch-1",
    ):
        selected_route = route or _route()
        self.route_observation = _identity_observation(
            {"routes": [selected_route]}
        )
        self.transform_epoch = transform_epoch

    async def latest_optional(self, stream):
        if stream == "camera.rgbd.data_routes":
            return self.route_observation
        return None

    async def transform(
        self,
        *,
        from_frame,
        to_frame,
        at_us,
        max_extrapolation_us,
        session_epoch,
    ):
        return {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "at_us": at_us,
            "translation_m": [1.0, 2.0, 3.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": [
                {
                    "session_epoch": self.transform_epoch,
                    "extrapolated_by_us": min(10, max_extrapolation_us),
                }
            ],
        }


class _Capture:
    def __init__(self, *, calibration_boot_id: str = BOOT_ID):
        self.calls = 0
        now_us = time.time_ns() // 1000
        self.frame = SimpleNamespace(
            rgb=np.zeros((6, 8, 3), dtype=np.uint8),
            depth_m=np.full((6, 8), 0.5, dtype=np.float32),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 4.0, "cy": 3.0},
            timestamp_us=now_us,
            frame_number=7,
            camera_frame="camera_color",
            session_epoch="vio-epoch-1",
            world_frame="world-vio",
            calibration_revision="calibration-1",
            observations={
                "bundle": _identity_observation(),
                "calibration": {
                    **_identity_observation(),
                    "boot_id": calibration_boot_id,
                    "data": {
                        "calibration_revision": "calibration-1",
                    },
                },
                "body_pose": {
                    "provider_id": "localization.test",
                    "observed_at_us": now_us,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "data": {
                        "session_epoch": "vio-epoch-1",
                        "world_frame": "world-vio",
                    },
                },
                "vio_status": {
                    "provider_id": "localization.test",
                    "observed_at_us": now_us,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "data": {
                        "tracking_state": "TRACKING",
                        "session_epoch": "vio-epoch-1",
                    },
                },
                "capture": {"copy_attempt": 1},
            },
        )

    async def capture(self):
        self.calls += 1
        return self.frame


class _PointingSkill:
    async def run(self, question: str) -> str:
        return question


class SpatialRegistrationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_enforced_current_binding_registers_without_physical_action(
        self,
    ) -> None:
        manager = _Manager()
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(),
            manager=manager,
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="ENFORCED",
            generic_route_mode="ENFORCED",
        )

        result = await adapter.run(
            pixel_yx=[3, 4],
            target_frame="world-vio",
            depth_policy="ROBUST_MEDIAN",
        )

        self.assertEqual(result["target_point_m"], [1.0, 2.0, 3.5])
        self.assertFalse(result["physical_action_submitted"])
        self.assertEqual(result["capability_binding"]["enforcement_issues"], [])
        self.assertEqual(
            manager.bind_requests[0]["required_capabilities"],
            list(CAMERA_CAPABILITIES),
        )
        expected_fallback = {
            capability: PROVIDER_ID for capability in CAMERA_CAPABILITIES
        }
        self.assertEqual(
            manager.bind_requests[0]["fallback_provider_ids"],
            expected_fallback,
        )
        self.assertEqual(
            result["capability_binding"][
                "configured_fallback_provider_ids"
            ],
            expected_fallback,
        )
        self.assertEqual(
            result["selected_route_metadata"]["valid_region"]["width"],
            6,
        )
        self.assertEqual(
            result["input_temporal_evidence"]["policy_id"],
            "spatial.registration.rgbd.input.v1",
        )
        self.assertEqual(
            result["input_temporal_evidence"]["evaluated_inputs"]["bundle"][
                "decision"
            ],
            "ACCEPT",
        )

    async def test_enforced_binding_rejects_cold_explicit_provider_fallback(
        self,
    ) -> None:
        cold = _binding(validity="FALLBACK_REQUIRES_ACTIVATION")
        for selection in cold["selections"]:
            selection["available"] = False
            selection["compatibility_verified"] = False
            selection["requires_activation"] = True
            selection["selection_reason"] = "EXPLICIT_PROVIDER_FALLBACK"
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(),
            manager=_Manager([cold]),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="ENFORCED",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "CAMERA_CAPABILITY_NOT_AVAILABLE",
        ):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_enforced_binding_rejects_provider_restart_before_registration(
        self,
    ) -> None:
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(),
            manager=_Manager(
                [
                    _binding(),
                    _binding(
                        validity="STALE_PROVIDER_RESTARTED",
                        boot_id="camera-boot-2",
                    ),
                ]
            ),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="ENFORCED",
        )

        with self.assertRaisesRegex(RuntimeError, "binding enforcement failed"):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_data_plane_identity_mismatch_is_rejected_in_shadow(self) -> None:
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(calibration_boot_id="old-camera-boot"),
            _Fabric(),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="SHADOW",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "calibration observation identity",
        ):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="NEAREST_VALID_PIXEL",
            )

    async def test_transform_epoch_mismatch_is_rejected(self) -> None:
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(transform_epoch="different-vio-epoch"),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="SHADOW",
        )

        with self.assertRaisesRegex(RuntimeError, "VIO session epoch"):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="CLOSEST_TO_CAMERA",
            )

    async def test_nontracking_vio_is_rejected(self) -> None:
        capture = _Capture()
        capture.frame.observations["vio_status"]["data"][
            "tracking_state"
        ] = "DEGRADED"
        adapter = SpatialRegistrationSkillAdapter(
            capture,
            _Fabric(),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="SHADOW",
        )

        with self.assertRaisesRegex(RuntimeError, "not in TRACKING"):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_producer_freshness_does_not_override_skill_policy(
        self,
    ) -> None:
        fabric = _Fabric()
        fabric.route_observation["freshness_ms"] = 100
        fabric.route_observation["received_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=2)
        ).isoformat()
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            fabric,
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="SHADOW",
        )

        result = await adapter.run(
            pixel_yx=[3, 4],
            target_frame="world-vio",
            depth_policy="ROBUST_MEDIAN",
        )

        route_evidence = result["input_temporal_evidence"][
            "evaluated_inputs"
        ]["route"]
        self.assertEqual(route_evidence["decision"], "ACCEPT")
        self.assertEqual(
            route_evidence["producer_recommended_max_age_ms"],
            100.0,
        )
        self.assertGreater(route_evidence["receipt_age_ms"], 1000.0)

    async def test_skill_temporal_policy_rejects_old_route(self) -> None:
        fabric = _Fabric()
        fabric.route_observation["observed_at_us"] = (
            time.time_ns() // 1000 - 16_000_000
        )
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            fabric,
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="SHADOW",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "exceeds Skill temporal policy",
        ):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_route_calibration_revision_mismatch_is_rejected(
        self,
    ) -> None:
        route = _route()
        route["products"]["calibration"]["revision"] = "calibration-2"
        route["alignments"][0]["calibration_revision"] = "calibration-2"
        adapter = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(route=route),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            binding_mode="ENFORCED",
            generic_route_mode="ENFORCED",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "route calibration revision",
        ):
            await adapter.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_direct_route_remains_available_unless_generic_is_enforced(
        self,
    ) -> None:
        shadow = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(route=_route(generic=False)),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            generic_route_mode="SHADOW",
        )
        result = await shadow.run(
            pixel_yx=[3, 4],
            target_frame="world-vio",
            depth_policy="ROBUST_MEDIAN",
        )
        self.assertTrue(result["data_route"]["hardware_specific"])

        enforced = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(route=_route(generic=False)),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
            generic_route_mode="ENFORCED",
        )
        with self.assertRaisesRegex(RuntimeError, "direct provider fallback"):
            await enforced.run(
                pixel_yx=[3, 4],
                target_frame="world-vio",
                depth_policy="ROBUST_MEDIAN",
            )

    async def test_agent_tool_uses_its_own_manifest_schema(self) -> None:
        spatial = SpatialRegistrationSkillAdapter(
            _Capture(),
            _Fabric(),
            manager=_Manager(),
            fallback_camera_provider_id=PROVIDER_ID,
        )
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=Path(__file__).resolve().parents[3],
            eligible_tool_names={
                "identify_pointed_object",
                "register_rgbd_pixel_to_world",
            },
            spatial_registration_skill=spatial,
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        result = await tools["register_rgbd_pixel_to_world"].on_invoke_tool(
            None,  # type: ignore[arg-type]
            json.dumps(
                {
                    "pixel_yx": [3, 4],
                    "target_frame": "world-vio",
                    "depth_policy": "ROBUST_MEDIAN",
                }
            ),
        )
        parsed = json.loads(result)
        self.assertFalse(parsed["physical_action_submitted"])

        with self.assertRaises(ValidationError):
            await tools["identify_pointed_object"].on_invoke_tool(
                None,  # type: ignore[arg-type]
                json.dumps(
                    {
                        "pixel_yx": [3, 4],
                        "target_frame": "world-vio",
                        "depth_policy": "ROBUST_MEDIAN",
                    }
                ),
            )


if __name__ == "__main__":
    unittest.main()
