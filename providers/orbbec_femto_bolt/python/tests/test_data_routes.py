from __future__ import annotations

import unittest

from orbbec_femto_provider.data_routes import (
    DIRECT_RGBD_ROUTE_CAPABILITY,
    DIRECT_RGBD_ROUTE_ID,
    GENERIC_RGBD_ROUTE_CAPABILITY,
    GENERIC_RGBD_ROUTE_ID,
    build_direct_rgbd_route,
    build_generic_rgbd_route,
    build_rgbd_route_set,
)


class DirectRgbdRouteTests(unittest.TestCase):
    @staticmethod
    def _reference(
        *,
        width: int,
        height: int,
        stride_bytes: int,
        format_name: str,
        depth_scale_mm: float = 0.0,
    ) -> dict:
        return {
            "width": width,
            "height": height,
            "stride_bytes": stride_bytes,
            "format": 1,
            "format_name": format_name,
            "bytes_per_pixel": max(1, stride_bytes // width),
            "depth_value_scale_mm": depth_scale_mm,
        }

    def test_route_preserves_explicit_orbbec_fallback(self) -> None:
        route = build_direct_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision="calibration-1",
            rgb_ready=True,
            depth_ready=True,
        )

        self.assertEqual(route["route_id"], DIRECT_RGBD_ROUTE_ID)
        self.assertEqual(route["capability"], DIRECT_RGBD_ROUTE_CAPABILITY)
        self.assertTrue(route["available"])
        self.assertTrue(route["hardware_specific"])
        self.assertEqual(route["selection"]["role"], "COMPATIBILITY_FALLBACK")
        self.assertTrue(route["selection"]["generic_route_preferred_when_compatible"])
        self.assertEqual(
            route["selection"]["explicit_provider_id"],
            "camera.femto_bolt",
        )
        self.assertEqual(
            route["transport"]["kind"],
            "WINDOWS_NAMED_SHARED_MEMORY",
        )
        self.assertEqual(
            route["products"]["rgb"]["coordinate_axis_names"],
            {
                "x": "camera_system_x",
                "y": "camera_system_y",
                "z": "camera_system_z",
            },
        )

    def test_route_is_advertised_but_unavailable_without_both_frames(self) -> None:
        route = build_direct_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision=None,
            rgb_ready=True,
            depth_ready=False,
        )

        self.assertFalse(route["available"])
        self.assertEqual(route["products"]["depth"]["units"], "millimeters")

    def test_generic_route_preserves_independent_channel_geometry(self) -> None:
        route = build_generic_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision="calibration-1",
            rgb_reference=self._reference(
                width=1920,
                height=1080,
                stride_bytes=5760,
                format_name="RGB",
            ),
            depth_reference=self._reference(
                width=640,
                height=576,
                stride_bytes=1280,
                format_name="Y16",
                depth_scale_mm=1.0,
            ),
            ir_reference=self._reference(
                width=1024,
                height=1024,
                stride_bytes=2048,
                format_name="Y16",
            ),
            aligned_depth_reference=self._reference(
                width=1920,
                height=1080,
                stride_bytes=3840,
                format_name="Y16",
                depth_scale_mm=1.0,
            ),
            custom_alignment={
                "implementation": "ORBBEC_ALIGN_FILTER",
                "valid_boundary": {"x": 120, "y": 40, "width": 1600, "height": 980},
            },
        )

        self.assertEqual(route["route_id"], GENERIC_RGBD_ROUTE_ID)
        self.assertEqual(route["capability"], GENERIC_RGBD_ROUTE_CAPABILITY)
        self.assertTrue(route["available"])
        self.assertFalse(route["hardware_specific"])
        self.assertEqual(route["selection"]["role"], "PRIMARY")
        channels = route["products"]["channels"]
        self.assertEqual(
            channels["rgb"]["coordinate_axis_names"],
            {
                "x": "camera_system_x",
                "y": "camera_system_y",
                "z": "camera_system_z",
            },
        )
        self.assertEqual(channels["rgb"]["native_grid"]["width"], 1920)
        self.assertEqual(channels["depth"]["native_grid"]["width"], 640)
        self.assertEqual(channels["infrared"]["native_grid"]["height"], 1024)
        self.assertEqual(
            channels["depth_registered_to_rgb"]["native_grid"]["width"],
            1920,
        )
        alignment = route["alignments"][0]
        self.assertTrue(alignment["allows_source_target_resolution_mismatch"])
        self.assertTrue(alignment["allows_source_target_aspect_ratio_mismatch"])
        self.assertTrue(alignment["allows_non_overlapping_boundaries"])
        self.assertEqual(
            alignment["provider_metadata"]["valid_boundary"]["x"],
            120,
        )
        self.assertEqual(
            channels["depth_registered_to_rgb"]["valid_region"],
            {
                "kind": "RECTANGLE_IN_NATIVE_GRID",
                "x": 120,
                "y": 40,
                "width": 1600,
                "height": 980,
                "outside_region": "INVALID",
                "source": "PROVIDER_OBSERVED_NONZERO_DEPTH_BOUNDARY",
            },
        )
        self.assertEqual(
            route["payload_policy"]["large_payload_transport"],
            "SHARED_MEMORY",
        )
        self.assertFalse(route["transport"]["large_payloads_on_fabric"])

    def test_generic_route_can_advertise_before_frames_exist(self) -> None:
        route = build_generic_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision=None,
            rgb_reference=None,
            depth_reference=None,
        )

        self.assertFalse(route["available"])
        self.assertFalse(route["products"]["channels"]["rgb"]["available"])
        self.assertFalse(route["alignments"][0]["available"])
        self.assertTrue(route["selection"]["coexists_with_generic_route"])

    def test_route_set_keeps_generic_and_direct_fallback_atomically_visible(self) -> None:
        direct = build_direct_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision="calibration-1",
            rgb_ready=True,
            depth_ready=True,
        )
        generic = build_generic_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision="calibration-1",
            rgb_reference=self._reference(
                width=1920,
                height=1080,
                stride_bytes=5760,
                format_name="RGB",
            ),
            depth_reference=self._reference(
                width=640,
                height=576,
                stride_bytes=1280,
                format_name="Y16",
                depth_scale_mm=1.0,
            ),
        )

        route_set = build_rgbd_route_set(direct, generic)

        self.assertEqual(route_set["preferred_route_id"], GENERIC_RGBD_ROUTE_ID)
        self.assertEqual(
            route_set["compatibility_fallback_route_ids"],
            [DIRECT_RGBD_ROUTE_ID],
        )
        self.assertEqual(
            {route["route_id"] for route in route_set["routes"]},
            {GENERIC_RGBD_ROUTE_ID, DIRECT_RGBD_ROUTE_ID},
        )
        self.assertFalse(route_set["payload_policy"]["large_payloads_on_fabric"])

    def test_generic_route_clamps_provider_boundary_to_registered_grid(self) -> None:
        route = build_generic_rgbd_route(
            provider_id="camera.femto_bolt",
            provider_instance_id="instance-1",
            boot_id="boot-1",
            mapping_name=r"Local\FemtoBoltPipeline_CameraHost_v2",
            calibration_revision="calibration-1",
            rgb_reference=self._reference(
                width=1920,
                height=1080,
                stride_bytes=5760,
                format_name="RGB",
            ),
            depth_reference=self._reference(
                width=640,
                height=576,
                stride_bytes=1280,
                format_name="Y16",
            ),
            aligned_depth_reference=self._reference(
                width=1920,
                height=1080,
                stride_bytes=3840,
                format_name="Y16",
            ),
            custom_alignment={
                "valid_boundary": {
                    "x": 1800,
                    "y": 1000,
                    "width": 500,
                    "height": 500,
                }
            },
        )

        self.assertEqual(
            route["products"]["channels"]["depth_registered_to_rgb"][
                "valid_region"
            ],
            {
                "kind": "RECTANGLE_IN_NATIVE_GRID",
                "x": 1800,
                "y": 1000,
                "width": 120,
                "height": 80,
                "outside_region": "INVALID",
                "source": "PROVIDER_OBSERVED_NONZERO_DEPTH_BOUNDARY",
            },
        )


if __name__ == "__main__":
    unittest.main()
