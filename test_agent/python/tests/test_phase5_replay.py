from __future__ import annotations

import json
import mmap
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from orbbec_femto_provider.shared_memory_access import (
    STREAM_ALIGNED_DEPTH,
    STREAM_COLOR,
    STREAM_DEPTH,
    CameraSharedMemory,
)
from physical_agent_test.phase5_replay import (
    Phase5ReplayBundle,
    Phase5ReplayCaptureService,
    Phase5ReplayScenarioRunner,
    ReplaySharedMemoryMapping,
    capture_phase5_replay_bundle,
)
from physical_agent_test.spatial_registration_adapter import (
    CAMERA_CAPABILITIES,
    SpatialRegistrationSkillAdapter,
)
from stationary_world_arm_alignment.camera import RgbdCapture


pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Phase 5 replay exercises Windows named shared memory",
)


def _reference(
    *,
    mapping_name: str,
    stream_kind: int,
    frame_number: int,
    width: int,
    height: int,
    stride_bytes: int,
    bytes_per_pixel: int,
    format_name: str,
) -> dict:
    return {
        "transport": "windows_named_shared_memory",
        "mapping_name": mapping_name,
        "stream_kind": stream_kind,
        "stream_name": (
            "color" if stream_kind == STREAM_COLOR else "depth_aligned_to_color"
        ),
        "pool_id": f"{mapping_name}:{stream_kind}",
        "slot_id": 0,
        "generation": 2,
        "slot_offset": 0,
        "payload_offset": 0,
        "payload_bytes": width * height * bytes_per_pixel,
        "payload_capacity_bytes": width * height * bytes_per_pixel,
        "frame_number": frame_number,
        "host_qpc": 1000 + frame_number,
        "device_timestamp_us": 2000 + frame_number,
        "system_timestamp_us": 3000 + frame_number,
        "global_timestamp_us": 4000 + frame_number,
        "frame_type": 0,
        "format": 0,
        "format_name": format_name,
        "width": width,
        "height": height,
        "stride_bytes": stride_bytes,
        "bytes_per_pixel": bytes_per_pixel,
        "depth_value_scale_mm": 1.0,
        "flags": 0,
        "metadata_mask": 0,
        "frame_metadata": {},
        "note": "synthetic source",
    }


def _generic_route_fields(mapping_name: str) -> dict:
    registered_region = {"x": 0, "y": 0, "width": 4, "height": 2}

    def channel(
        channel_id: str,
        *,
        width: int,
        height: int,
        stride_bytes: int,
        format_name: str,
        valid_region: dict | None = None,
    ) -> dict:
        return {
            "available": True,
            "channel_id": channel_id,
            "coordinate_convention_id": (
                "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            ),
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

    return {
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "mapping_name": mapping_name,
            "consumer_library": "orbbec_femto_provider.shared_memory_access",
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
                "revision": "replay-calibration",
            },
            "channels": {
                "rgb": channel(
                    "rgb",
                    width=4,
                    height=2,
                    stride_bytes=12,
                    format_name="RGB",
                ),
                "infrared": channel(
                    "infrared",
                    width=2,
                    height=2,
                    stride_bytes=4,
                    format_name="Y16",
                ),
                "depth": channel(
                    "depth",
                    width=4,
                    height=2,
                    stride_bytes=8,
                    format_name="Y16",
                ),
                "depth_registered_to_rgb": channel(
                    "depth_registered_to_rgb",
                    width=4,
                    height=2,
                    stride_bytes=8,
                    format_name="Y16",
                    valid_region=registered_region,
                ),
            }
        },
        "synchronization": {
            "bundle_stream": "camera.rgbd.bundle",
            "policy": "TIMESTAMP_DELTA_WITH_PROVIDER_DECLARED_THRESHOLD",
        },
        "alignments": [
            {
                "alignment_id": "depth_to_rgb.replay.v1",
                "calibration_revision": "replay-calibration",
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


@unittest.skipUnless(hasattr(mmap, "ACCESS_WRITE"), "mmap is required")
class Phase5ReplayTests(unittest.TestCase):
    def _source(self):
        mapping_name = f"Local\\MidbrainPhase5TestSource_{id(self)}"
        rgb = bytes(range(24))
        depth = np.full((2, 4), 500, dtype="<u2").tobytes()
        source = ReplaySharedMemoryMapping(
            mapping_name=mapping_name,
            records=[
                (
                    "rgb",
                    _reference(
                        mapping_name=mapping_name,
                        stream_kind=STREAM_COLOR,
                        frame_number=11,
                        width=4,
                        height=2,
                        stride_bytes=12,
                        bytes_per_pixel=3,
                        format_name="RGB",
                    ),
                    rgb,
                ),
                (
                    "native_depth",
                    _reference(
                        mapping_name=mapping_name,
                        stream_kind=STREAM_DEPTH,
                        frame_number=10,
                        width=4,
                        height=2,
                        stride_bytes=8,
                        bytes_per_pixel=2,
                        format_name="Y16",
                    ),
                    depth,
                ),
                (
                    "registered_depth",
                    _reference(
                        mapping_name=mapping_name,
                        stream_kind=STREAM_ALIGNED_DEPTH,
                        frame_number=10,
                        width=4,
                        height=2,
                        stride_bytes=8,
                        bytes_per_pixel=2,
                        format_name="Y16",
                    ),
                    depth,
                ),
            ],
        )
        return source, rgb, depth

    def test_capture_and_materialize_uses_replay_only_shared_memory(self) -> None:
        source, rgb, depth = self._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            bundle_dir = Path(temp) / "bundle"
            capture_phase5_replay_bundle(
                bundle_directory=bundle_dir,
                route_set={
                    "preferred_route_id": "generic",
                    "routes": [
                        {
                            "route_id": "generic",
                            "transport": {
                                "mapping_name": source.mapping_name,
                            },
                        }
                    ],
                },
                references=source.references,
                records={
                    "binding": {"provider_id": "camera.live"},
                    "authorization": {"decision": "NOT_REQUESTED"},
                },
                bundle_id="roundtrip",
            )
            with Phase5ReplayBundle.load(bundle_dir) as bundle:
                provenance = bundle.provenance_summary()
                self.assertEqual("roundtrip", provenance["bundle_id"])
                self.assertEqual(
                    "physical_agent.phase5_replay_provenance_summary",
                    provenance["schema"],
                )
                self.assertTrue(provenance["manifest_sha256"])
                self.assertTrue(provenance["secret_redaction_applied"])
                self.assertFalse(
                    provenance["replay_isolation"][
                        "physical_controller_call_allowed"
                    ]
                )
                self.assertEqual(
                    "MANUAL_REVIEW",
                    provenance["retention"]["policy"],
                )
                self.assertFalse(
                    provenance["retention"]["automatic_deletion_allowed"]
                )
                replay = bundle.materialize()
                self.assertFalse(
                    replay.replay_policy["physical_controller_call_allowed"]
                )
                self.assertEqual("roundtrip", replay.bundle_id)
                replay_mapping = replay.references["rgb"]["mapping_name"]
                self.assertNotEqual(source.mapping_name, replay_mapping)
                self.assertEqual(
                    replay_mapping,
                    replay.route_set["routes"][0]["transport"]["mapping_name"],
                )
                camera = CameraSharedMemory(replay_mapping).open()
                try:
                    self.assertEqual(
                        rgb,
                        camera.read_ref(replay.references["rgb"]),
                    )
                    self.assertEqual(
                        depth,
                        camera.read_ref(
                            replay.references["registered_depth"]
                        ),
                    )
                finally:
                    camera.close()

    def test_capture_rejects_recycled_reference(self) -> None:
        source, _rgb, _depth = self._source()
        self.addCleanup(source.close)
        stale = dict(source.references["rgb"])
        stale["generation"] += 2
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "expired|recycled"):
                capture_phase5_replay_bundle(
                    bundle_directory=Path(temp) / "bundle",
                    route_set={},
                    references={"rgb": stale},
                    records={},
                )

    def test_load_rejects_payload_tampering(self) -> None:
        source, _rgb, _depth = self._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            bundle_dir = Path(temp) / "bundle"
            capture_phase5_replay_bundle(
                bundle_directory=bundle_dir,
                route_set={},
                references={"rgb": source.references["rgb"]},
                records={},
            )
            manifest = json.loads(
                (bundle_dir / "manifest.json").read_text(encoding="utf-8")
            )
            payload = bundle_dir / manifest["payloads"]["rgb"]["path"]
            payload.write_bytes(payload.read_bytes() + b"x")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                Phase5ReplayBundle.load(bundle_dir)

    def test_capture_redacts_secrets_but_preserves_decision_evidence(self) -> None:
        source, _rgb, _depth = self._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            bundle_dir = Path(temp) / "bundle"
            capture_phase5_replay_bundle(
                bundle_directory=bundle_dir,
                route_set={},
                references={"rgb": source.references["rgb"]},
                records={
                    "api_key": "secret",
                    "authorization": {
                        "decision": "APPROVED",
                        "access_token": "secret-token",
                    },
                },
            )
            bundle = Phase5ReplayBundle.load(bundle_dir)
            self.assertEqual("[REDACTED]", bundle.manifest["records"]["api_key"])
            self.assertEqual(
                "APPROVED",
                bundle.manifest["records"]["authorization"]["decision"],
            )
            self.assertEqual(
                "[REDACTED]",
                bundle.manifest["records"]["authorization"]["access_token"],
            )


class _FakeFabric:
    def __init__(self, source: ReplaySharedMemoryMapping):
        self.source = source

    async def latest(self, stream: str) -> dict:
        if stream == "camera.rgbd.data_routes":
            return {
                "provider_id": "camera.test",
                "data": {
                    "routes": [
                        {
                            "route_id": "generic",
                            "transport": {
                                "mapping_name": self.source.mapping_name,
                            },
                        }
                    ]
                },
            }
        if stream == "camera.rgbd.bundle":
            return {
                "provider_id": "camera.test",
                "data": {
                    "rgb": self.source.references["rgb"],
                    "depth": self.source.references["native_depth"],
                    "depth_aligned_to_rgb": (
                        self.source.references["registered_depth"]
                    ),
                },
            }
        raise AssertionError(f"unexpected required stream: {stream}")

    async def latest_optional(self, _stream: str):
        return None


class Phase5ReplayCaptureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_captures_and_validates_without_control_access(
        self,
    ) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            service = Phase5ReplayCaptureService(
                _FakeFabric(source),
                Path(temp),
            )
            captured = await service.capture_current(
                bundle_id="service-test",
                additional_records={"binding": {"validity": "CURRENT"}},
            )
            self.assertEqual("CAPTURED", captured["status"])
            self.assertFalse(
                captured["replay_policy"]["physical_controller_call_allowed"]
            )
            validated = await service.validate_bundle("service-test")
            self.assertEqual("VALID", validated["status"])
            self.assertFalse(validated["hardware_access_allowed"])
            self.assertEqual(
                {"rgb", "native_depth", "registered_depth"},
                set(validated["verified_payloads"]),
            )
            listed = service.list_bundles()
            self.assertEqual("VALID", listed[0]["status"])
            self.assertEqual(
                "service-test",
                listed[0]["provenance"]["bundle_id"],
            )
            provenance = service.bundle_provenance("service-test")
            self.assertEqual("service-test", provenance["bundle_id"])
            self.assertEqual(
                {"native_depth", "registered_depth", "rgb"},
                set(provenance["payloads"]),
            )
            self.assertTrue(provenance["record_presence"]["manager_binding"])

            recycled = await service.run_scenario(
                "service-test",
                "recycled_bufferref",
            )
            self.assertEqual(
                "REJECT_RECYCLED_BUFFERREF",
                recycled["expected_outcome"],
            )
            self.assertTrue(recycled["observed"]["rejected"])

    async def test_capture_rejects_invalid_retention_rule(self) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "retention review"):
                capture_phase5_replay_bundle(
                    bundle_directory=Path(temp) / "invalid-retention",
                    route_set={},
                    references={"rgb": source.references["rgb"]},
                    records={},
                    retention_review_days=0,
                )

    async def test_all_control_plane_scenarios_are_hardware_incapable(
        self,
    ) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            service = Phase5ReplayCaptureService(
                _FakeFabric(source),
                Path(temp),
            )
            await service.capture_current(bundle_id="scenarios")
            for scenario_name in sorted(
                Phase5ReplayScenarioRunner.SCENARIOS
                - {"recycled_bufferref"}
            ):
                result = await service.run_scenario(
                    "scenarios",
                    scenario_name,
                )
                self.assertEqual("SCENARIO_COMPLETED", result["status"])
                self.assertEqual(
                    result["expected_outcome"],
                    result["observed"]["outcome"],
                )
                self.assertFalse(
                    result["observed"]["physical_controller_called"]
                )
                self.assertFalse(
                    result["observed"]["physical_lease_acquired"]
                )
                self.assertFalse(
                    result["observed"]["hardware_provider_started"]
                )
                self.assertFalse(result["hardware_access_allowed"])
                self.assertFalse(result["physical_controller_call_allowed"])

    async def test_missing_phase5_scenarios_expose_measured_evidence(
        self,
    ) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            service = Phase5ReplayCaptureService(
                _FakeFabric(source),
                Path(temp),
            )
            await service.capture_current(bundle_id="missing-scenarios")

            stale_depth = await service.run_scenario(
                "missing-scenarios",
                "stale_depth",
            )
            self.assertGreater(
                stale_depth["observed"]["evidence"]["age_us"],
                stale_depth["observed"]["evidence"][
                    "maximum_channel_skew_us"
                ],
            )

            independent = await service.run_scenario(
                "missing-scenarios",
                "independent_channel_rates",
            )
            self.assertEqual("PASS", independent["observed"]["outcome"])
            self.assertTrue(
                independent["observed"]["evidence"]["independent_frames"]
            )
            self.assertLessEqual(
                independent["observed"]["evidence"]["timestamp_skew_us"],
                independent["observed"]["evidence"][
                    "maximum_channel_skew_us"
                ],
            )

            geometry = await service.run_scenario(
                "missing-scenarios",
                "flexible_channel_geometry",
            )
            self.assertEqual("PASS", geometry["observed"]["outcome"])
            self.assertTrue(
                geometry["observed"]["evidence"]["resolution_differs"]
            )
            self.assertTrue(
                geometry["observed"]["evidence"]["aspect_ratio_differs"]
            )
            self.assertTrue(
                geometry["observed"]["evidence"][
                    "registered_depth_matches_rgb"
                ]
            )

            alignment = await service.run_scenario(
                "missing-scenarios",
                "alignment_revision_change",
            )
            self.assertEqual(
                "RECAPTURE_REQUIRED",
                alignment["observed"]["outcome"],
            )

            preview = await service.run_scenario(
                "missing-scenarios",
                "rejected_controller_preview",
            )
            self.assertEqual(
                "AUTHORIZATION_NOT_CREATED",
                preview["observed"]["outcome"],
            )
            self.assertTrue(
                preview["observed"]["evidence"][
                    "physical_execution_blockers"
                ]
            )

            fencing = await service.run_scenario(
                "missing-scenarios",
                "stale_fencing_generation",
            )
            self.assertEqual(
                "MOTION_INHIBITED",
                fencing["observed"]["outcome"],
            )


class _ReplayBindingManager:
    async def bind_capabilities(self, _capabilities, **_kwargs):
        return {"binding_id": "replay-camera-binding"}

    async def capability_binding(self, binding_id):
        return {
            "binding_id": binding_id,
            "status": "RESOLVED",
            "validity": "CURRENT",
            "selections": [
                {
                    "capability": capability,
                    "provider_id": "camera.replay",
                    "provider_instance_id": "replay-instance",
                    "boot_id": "replay-boot",
                    "available": True,
                }
                for capability in CAMERA_CAPABILITIES
            ],
        }


class _ReplaySpatialFabric:
    def __init__(self, materialization):
        self.materialization = materialization
        identity = {
            "provider_id": "camera.replay",
            "provider_instance_id": "replay-instance",
            "boot_id": "replay-boot",
        }
        self.route_observation = {
            **identity,
            "observed_at_us": 4011,
            "data": materialization.route_set,
        }
        self.bundle_observation = {
            **identity,
            "observed_at_us": 4011,
            "data": {
                "rgb": materialization.references["rgb"],
                "depth": materialization.references["native_depth"],
                "depth_aligned_to_rgb": (
                    materialization.references["registered_depth"]
                ),
                "coordinate_conventions": {
                    "rgb": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                    "depth": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                    "aligned_depth": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                },
            },
        }
        self.calibration_observation = {
            **identity,
            "observed_at_us": 4011,
            "data": {
                "rgb_intrinsic": {
                    "fx": 100.0,
                    "fy": 100.0,
                    "cx": 2.0,
                    "cy": 1.0,
                },
                "calibration_revision": "replay-calibration",
                "coordinate_conventions": {
                    "color": (
                        "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                    ),
                },
            },
        }

    async def latest_optional(self, stream):
        if stream == "camera.rgbd.data_routes":
            return self.route_observation
        if stream == "camera.rgbd.bundle":
            return self.bundle_observation
        if stream == "camera.calibration":
            return self.calibration_observation
        if stream == "localization.body.pose":
            return {
                "provider_id": "localization.replay",
                "observed_at_us": 4011,
                "data": {
                    "world_frame": "replay-world",
                    "session_epoch": "replay-vio-epoch",
                    "convention_id": (
                        "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                    ),
                },
            }
        if stream == "localization.vio.status":
            return {
                "provider_id": "localization.replay",
                "observed_at_us": 4011,
                "data": {
                    "tracking_state": "TRACKING",
                    "session_epoch": "replay-vio-epoch",
                    "convention_id": (
                        "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                    ),
                },
            }
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
                    "session_epoch": session_epoch,
                    "extrapolated_by_us": min(1, max_extrapolation_us),
                }
            ],
        }


@unittest.skipUnless(hasattr(mmap, "ACCESS_WRITE"), "mmap is required")
class Phase5SpatialReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_and_direct_routes_match_on_same_replay_payload(
        self,
    ) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            replay_root = Path(temp)
            bundle_dir = replay_root / "route-comparison"
            routes = []
            for generic in (True, False):
                routes.append(
                    {
                        "route_id": "generic-replay" if generic else "direct-replay",
                        "capability": (
                            "camera.rgbd.route.generic_shared_memory"
                            if generic
                            else "camera.rgbd.route.direct_shared_memory"
                        ),
                        "provider_id": "camera.replay",
                        "provider_instance_id": "replay-instance",
                        "boot_id": "replay-boot",
                        "available": True,
                        "hardware_specific": not generic,
                        "selection": {
                            "role": "PRIMARY" if generic else "COMPATIBILITY_FALLBACK"
                        },
                        "transport": {
                            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
                            "mapping_name": source.mapping_name,
                        },
                        "products": {
                            "channels": {
                                "depth_registered_to_rgb": {
                                    "valid_region": {
                                        "x": 0,
                                        "y": 0,
                                        "width": 4,
                                        "height": 2,
                                    }
                                }
                            }
                        },
                    }
                )
            capture_phase5_replay_bundle(
                bundle_directory=bundle_dir,
                route_set={"routes": routes},
                references=source.references,
                records={
                    "fabric": {
                        "optional_streams": {
                            "camera.calibration": {
                                "coordinate_frame": "replay-camera",
                                "calibration_revision": "replay-calibration",
                                "data": {
                                    "rgb_intrinsic": {
                                        "fx": 100.0,
                                        "fy": 100.0,
                                        "cx": 2.0,
                                        "cy": 1.0,
                                    },
                                    "calibration_revision": "replay-calibration",
                                },
                            }
                        }
                    }
                },
                bundle_id="route-comparison",
            )
            service = Phase5ReplayCaptureService(None, replay_root)

            result = await service.compare_rgbd_routes(
                "route-comparison",
                pixel_yx=(1.0, 2.0),
                depth_policy="ROBUST_MEDIAN",
            )

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["equivalent"])
            self.assertEqual(result["maximum_absolute_point_delta_m"], 0.0)
            self.assertEqual(result["depth_delta_m"], 0.0)
            self.assertTrue(result["same_registered_depth_pixel"])
            self.assertFalse(result["hardware_access_allowed"])
            self.assertFalse(result["physical_action_submitted"])
            self.assertEqual(
                result["results"]["generic"]["data_route"]["capability"],
                "camera.rgbd.route.generic_shared_memory",
            )
            self.assertEqual(
                result["results"]["direct"]["data_route"]["capability"],
                "camera.rgbd.route.direct_shared_memory",
            )

    async def test_enforced_spatial_skill_consumes_replay_shared_memory(
        self,
    ) -> None:
        source, _rgb, _depth = Phase5ReplayTests()._source()
        self.addCleanup(source.close)
        with tempfile.TemporaryDirectory() as temp:
            bundle_dir = Path(temp) / "spatial-replay"
            route = {
                "route_id": "generic-replay",
                "capability": "camera.rgbd.route.generic_shared_memory",
                "provider_id": "camera.replay",
                "provider_instance_id": "replay-instance",
                "boot_id": "replay-boot",
                "available": True,
                "hardware_specific": False,
                "selection": {"role": "PRIMARY"},
                **_generic_route_fields(source.mapping_name),
            }
            capture_phase5_replay_bundle(
                bundle_directory=bundle_dir,
                route_set={"routes": [route]},
                references=source.references,
                records={"test": "enforced spatial replay"},
                bundle_id="spatial-enforced",
            )
            with Phase5ReplayBundle.load(bundle_dir) as bundle:
                materialization = bundle.materialize()
                fabric = _ReplaySpatialFabric(materialization)
                adapter = SpatialRegistrationSkillAdapter(
                    RgbdCapture(fabric, "replay-camera-frame"),
                    fabric,
                    manager=_ReplayBindingManager(),
                    fallback_camera_provider_id="camera.replay",
                    binding_mode="ENFORCED",
                    generic_route_mode="ENFORCED",
                    maximum_source_age_ms={
                        "route": None,
                        "bundle": None,
                        "calibration": None,
                        "body_pose": None,
                        "vio_status": None,
                    },
                )

                result = await adapter.run(
                    pixel_yx=[1, 2],
                    target_frame="replay-world",
                    depth_policy="ROBUST_MEDIAN",
                )

                self.assertEqual(result["target_point_m"], [1.0, 2.0, 3.5])
                self.assertFalse(result["physical_action_submitted"])
                self.assertEqual(
                    result["input_temporal_evidence"][
                        "evaluated_inputs"
                    ]["bundle"]["observed_at_us"],
                    4011,
                )
                self.assertIsNone(
                    result["input_temporal_evidence"][
                        "evaluated_inputs"
                    ]["bundle"]["maximum_source_age_ms"],
                )
                self.assertTrue(
                    materialization.route_set["replay"]["active"]
                )
                self.assertFalse(
                    materialization.replay_policy[
                        "physical_controller_call_allowed"
                    ]
                )


if __name__ == "__main__":
    unittest.main()
