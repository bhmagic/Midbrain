from __future__ import annotations

import unittest

from physical_agent_test.route_resolver import (
    DIRECT_RGBD_ROUTE_CAPABILITY,
    GENERIC_RGBD_ROUTE_CAPABILITY,
    generic_rgbd_route_issues,
    routes_from_observation,
    select_rgbd_route,
)


def _channel(
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
        "native_grid": {
            "width": width,
            "height": height,
            "stride_bytes": stride_bytes,
            "independent_resolution": True,
            "independent_aspect_ratio": True,
        },
        "valid_region": valid_region or {
            "x": 0,
            "y": 0,
            "width": width,
            "height": height,
        },
        "timestamp": {
            "selection_order": [
                "global_timestamp_us",
                "system_timestamp_us",
                "device_timestamp_us",
            ],
        },
        "sample": {"format_name": format_name},
        "calibration": {
            "stream": "camera.calibration",
            "document_path": (
                "rgb_intrinsic"
                if channel_id in {"rgb", "depth_registered_to_rgb"}
                else f"{channel_id}.intrinsic"
            ),
        },
    }


def _generic_descriptor_fields(
    *,
    mapping_name: str = "Local\\FlexibleRgbdTest",
    consumer_library: str = "orbbec_femto_provider.shared_memory_access",
) -> dict:
    registered_region = {
        "x": 80,
        "y": 12,
        "width": 1100,
        "height": 690,
    }
    return {
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "mapping_name": mapping_name,
            "consumer_library": consumer_library,
            "buffer_reference_schema": "physical_agent.buffer_ref",
            "payload_location": "SHARED_MEMORY_ONLY",
            "large_payloads_on_fabric": False,
            "provider_specific_layout": True,
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
                "revision": "synthetic-calibration-1",
            },
            "channels": {
                "rgb": _channel(
                    "rgb",
                    width=1280,
                    height=720,
                    stride_bytes=256,
                    format_name="MJPG",
                ),
                "infrared": _channel(
                    "infrared",
                    width=848,
                    height=480,
                    stride_bytes=1696,
                    format_name="Y16",
                ),
                "depth": _channel(
                    "depth",
                    width=640,
                    height=480,
                    stride_bytes=1280,
                    format_name="Y16",
                ),
                "depth_registered_to_rgb": _channel(
                    "depth_registered_to_rgb",
                    width=1280,
                    height=720,
                    stride_bytes=2560,
                    format_name="Y16",
                    valid_region=registered_region,
                ),
            },
        },
        "synchronization": {
            "bundle_stream": "camera.rgbd.bundle",
            "policy": "TIMESTAMP_DELTA_WITH_PROVIDER_DECLARED_THRESHOLD",
        },
        "alignments": [
            {
                "alignment_id": "depth_to_rgb.synthetic_custom.v1",
                "calibration_revision": "synthetic-calibration-1",
                "source_channel": "depth",
                "target_channel": "rgb",
                "output_channel": "depth_registered_to_rgb",
                "output_grid": "TARGET_CHANNEL_NATIVE_GRID",
                "producer": "PROVIDER_CUSTOM",
                "allows_source_target_resolution_mismatch": True,
                "allows_source_target_aspect_ratio_mismatch": True,
                "allows_non_overlapping_boundaries": True,
                "boundary_model": {
                    "output_valid_region": dict(registered_region),
                },
            },
        ],
    }


def _route(
    capability: str,
    *,
    provider_id: str = "camera.femto_bolt",
    available: bool = True,
) -> dict:
    generic = capability == GENERIC_RGBD_ROUTE_CAPABILITY
    route = {
        "route_id": "generic" if generic else "orbbec-direct",
        "capability": capability,
        "provider_id": provider_id,
        "provider_instance_id": "instance-1",
        "boot_id": "boot-1",
        "available": available,
        "hardware_specific": not generic,
        "selection": {
            "role": "PRIMARY" if generic else "COMPATIBILITY_FALLBACK",
        },
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "consumer_library": "orbbec_femto_provider.shared_memory_access",
        },
    }
    if generic:
        route.update(_generic_descriptor_fields())
    return route


class RouteResolverTests(unittest.TestCase):
    def test_generic_route_is_preferred_over_brand_fallback(self) -> None:
        selected = select_rgbd_route(
            [
                _route(DIRECT_RGBD_ROUTE_CAPABILITY),
                _route(GENERIC_RGBD_ROUTE_CAPABILITY),
            ],
            provider_id="camera.femto_bolt",
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.capability, GENERIC_RGBD_ROUTE_CAPABILITY)
        self.assertEqual(selected.selection_reason, "GENERIC_ROUTE_PREFERRED")

    def test_direct_route_remains_available_as_fallback(self) -> None:
        selected = select_rgbd_route(
            [_route(DIRECT_RGBD_ROUTE_CAPABILITY)],
            provider_id="camera.femto_bolt",
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.capability, DIRECT_RGBD_ROUTE_CAPABILITY)
        self.assertTrue(selected.hardware_specific)
        self.assertEqual(
            selected.selection_reason,
            "EXPLICIT_PROVIDER_COMPATIBILITY_FALLBACK",
        )

    def test_provider_binding_prevents_cross_provider_capture(self) -> None:
        selected = select_rgbd_route(
            [_route(GENERIC_RGBD_ROUTE_CAPABILITY, provider_id="camera.other")],
            provider_id="camera.femto_bolt",
        )

        self.assertIsNone(selected)

    def test_non_orbbec_descriptor_supports_independent_channel_geometry(
        self,
    ) -> None:
        route = _route(
            GENERIC_RGBD_ROUTE_CAPABILITY,
            provider_id="camera.synthetic.flexible",
        )
        route["route_id"] = "camera.synthetic.flexible.rgbd.v1"
        route.update(
            _generic_descriptor_fields(
                mapping_name="Local\\SyntheticFlexibleRgbd",
                consumer_library="synthetic_rgbd.shared_memory_access",
            )
        )

        self.assertEqual(generic_rgbd_route_issues(route), ())
        selected = select_rgbd_route(
            [route],
            provider_id="camera.synthetic.flexible",
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.capability, GENERIC_RGBD_ROUTE_CAPABILITY)
        self.assertEqual(
            selected.consumer_library,
            "synthetic_rgbd.shared_memory_access",
        )
        channels = route["products"]["channels"]
        self.assertNotEqual(
            channels["rgb"]["native_grid"],
            channels["depth"]["native_grid"],
        )
        self.assertNotEqual(
            channels["infrared"]["native_grid"],
            channels["depth_registered_to_rgb"]["native_grid"],
        )

    def test_invalid_generic_descriptor_uses_visible_direct_fallback(
        self,
    ) -> None:
        generic = _route(GENERIC_RGBD_ROUTE_CAPABILITY)
        generic["products"]["channels"]["depth_registered_to_rgb"][
            "valid_region"
        ]["width"] = 5000

        selected = select_rgbd_route(
            [
                generic,
                _route(DIRECT_RGBD_ROUTE_CAPABILITY),
            ],
            provider_id="camera.femto_bolt",
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.capability, DIRECT_RGBD_ROUTE_CAPABILITY)
        self.assertEqual(
            selected.selection_reason,
            "INVALID_GENERIC_ROUTE_EXPLICIT_PROVIDER_FALLBACK",
        )
        self.assertTrue(
            any(
                "VALID_REGION_OUT_OF_BOUNDS" in issue
                for issue in selected.rejected_generic_route_issues
            )
        )

    def test_route_set_and_single_route_observations_are_supported(self) -> None:
        direct = _route(DIRECT_RGBD_ROUTE_CAPABILITY)
        generic = _route(GENERIC_RGBD_ROUTE_CAPABILITY)

        self.assertEqual(
            routes_from_observation({"data": generic}),
            [generic],
        )
        self.assertEqual(
            routes_from_observation({"data": {"routes": [direct, generic]}}),
            [direct, generic],
        )


if __name__ == "__main__":
    unittest.main()
