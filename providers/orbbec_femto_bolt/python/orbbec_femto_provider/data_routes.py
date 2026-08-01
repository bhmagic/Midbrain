from __future__ import annotations

import copy
from typing import Any


DIRECT_RGBD_ROUTE_ID = "camera.femto_bolt.rgbd.windows_shared_memory.v2"
DIRECT_RGBD_ROUTE_CAPABILITY = "camera.rgbd.route.direct_shared_memory"
GENERIC_RGBD_ROUTE_ID = "camera.rgbd.shared_memory.flexible.v1"
GENERIC_RGBD_ROUTE_CAPABILITY = "camera.rgbd.route.generic_shared_memory"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)
CAMERA_SYSTEM_AXIS_NAMES = {
    "x": "camera_system_x",
    "y": "camera_system_y",
    "z": "camera_system_z",
}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _channel_descriptor(
    *,
    channel_id: str,
    role: str,
    reference_stream: str,
    coordinate_frame: str,
    reference: dict[str, Any] | None,
    units: str | None,
    calibration_path: str,
) -> dict[str, Any]:
    ref = reference or {}
    width = _positive_int(ref.get("width"))
    height = _positive_int(ref.get("height"))
    stride_bytes = _positive_int(ref.get("stride_bytes"))
    descriptor: dict[str, Any] = {
        "channel_id": channel_id,
        "role": role,
        "reference_stream": reference_stream,
        "reference_schema": "physical_agent.buffer_ref",
        "coordinate_frame": coordinate_frame,
        "coordinate_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
        "coordinate_axis_names": dict(CAMERA_SYSTEM_AXIS_NAMES),
        "available": bool(reference and width and height),
        "native_grid": {
            "width": width,
            "height": height,
            "stride_bytes": stride_bytes,
            "independent_resolution": True,
            "independent_aspect_ratio": True,
        },
        "sample": {
            "format_code": ref.get("format"),
            "format_name": ref.get("format_name"),
            "bytes_per_pixel": _positive_int(ref.get("bytes_per_pixel")),
            "units": units,
            "value_scale": (
                _finite_float(ref.get("depth_value_scale_mm"))
                if units == "millimeters"
                else None
            ),
        },
        "valid_region": {
            "kind": "RECTANGLE_IN_NATIVE_GRID",
            "x": 0,
            "y": 0,
            "width": width,
            "height": height,
            "outside_region": "INVALID",
        },
        "calibration": {
            "stream": "camera.calibration",
            "document_path": calibration_path,
        },
        "timestamp": {
            "selection_order": [
                "global_timestamp_us",
                "system_timestamp_us",
                "device_timestamp_us",
            ],
            "frame_number_field": "frame_number",
        },
    }
    return descriptor


def build_generic_rgbd_route(
    *,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
    mapping_name: str,
    calibration_revision: str | None,
    rgb_reference: dict[str, Any] | None,
    depth_reference: dict[str, Any] | None,
    ir_reference: dict[str, Any] | None = None,
    aligned_depth_reference: dict[str, Any] | None = None,
    custom_alignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe independent camera grids over a shared-memory payload route."""

    channels = {
        "rgb": _channel_descriptor(
            channel_id="rgb",
            role="COLOR",
            reference_stream="camera.rgb.frame_ref",
            coordinate_frame="femto_bolt_color_optical_frame",
            reference=rgb_reference,
            units=None,
            calibration_path="rgb_intrinsic",
        ),
        "depth": _channel_descriptor(
            channel_id="depth",
            role="DEPTH",
            reference_stream="camera.depth.frame_ref",
            coordinate_frame="femto_bolt_depth_optical_frame",
            reference=depth_reference,
            units="millimeters",
            calibration_path="depth_intrinsic",
        ),
        "infrared": _channel_descriptor(
            channel_id="infrared",
            role="INFRARED",
            reference_stream="camera.ir.frame_ref",
            coordinate_frame="femto_bolt_ir_optical_frame",
            reference=ir_reference,
            units=None,
            calibration_path="infrared.intrinsic",
        ),
        "depth_registered_to_rgb": _channel_descriptor(
            channel_id="depth_registered_to_rgb",
            role="REGISTERED_DEPTH",
            reference_stream="camera.depth_aligned_to_rgb.frame_ref",
            coordinate_frame="femto_bolt_color_optical_frame",
            reference=aligned_depth_reference,
            units="millimeters",
            calibration_path="rgb_intrinsic",
        ),
    }
    if custom_alignment and isinstance(
        custom_alignment.get("valid_boundary"),
        dict,
    ):
        registered_channel = channels["depth_registered_to_rgb"]
        grid = registered_channel["native_grid"]
        grid_width = _positive_int(grid.get("width"))
        grid_height = _positive_int(grid.get("height"))
        boundary = custom_alignment["valid_boundary"]
        boundary_x = max(0, int(boundary.get("x") or 0))
        boundary_y = max(0, int(boundary.get("y") or 0))
        boundary_width = _positive_int(boundary.get("width"))
        boundary_height = _positive_int(boundary.get("height"))
        if (
            grid_width
            and grid_height
            and boundary_width
            and boundary_height
            and boundary_x < grid_width
            and boundary_y < grid_height
        ):
            registered_channel["valid_region"] = {
                "kind": "RECTANGLE_IN_NATIVE_GRID",
                "x": boundary_x,
                "y": boundary_y,
                "width": min(boundary_width, grid_width - boundary_x),
                "height": min(boundary_height, grid_height - boundary_y),
                "outside_region": "INVALID",
                "source": "PROVIDER_OBSERVED_NONZERO_DEPTH_BOUNDARY",
            }
    alignment = {
        "alignment_id": "depth_to_rgb.provider_custom.v1",
        "source_channel": "depth",
        "target_channel": "rgb",
        "output_channel": "depth_registered_to_rgb",
        "producer": "PROVIDER_CUSTOM",
        "method": "SOFTWARE_DEPTH_TO_COLOR_RESAMPLING",
        "calibration_stream": "camera.calibration",
        "calibration_revision": calibration_revision,
        "extrinsic_path": "depth_to_color",
        "output_grid": "TARGET_CHANNEL_NATIVE_GRID",
        "allows_source_target_resolution_mismatch": True,
        "allows_source_target_aspect_ratio_mismatch": True,
        "allows_non_overlapping_boundaries": True,
        "boundary_model": {
            "output_valid_region": channels["depth_registered_to_rgb"]["valid_region"],
            "samples_outside_source_projection": "INVALID",
            "samples_outside_target_boundary": "OMITTED",
        },
        "available": channels["depth_registered_to_rgb"]["available"],
    }
    if custom_alignment:
        alignment["provider_metadata"] = copy.deepcopy(custom_alignment)

    available = bool(
        channels["rgb"]["available"] and channels["depth"]["available"]
    )
    return {
        "route_id": GENERIC_RGBD_ROUTE_ID,
        "capability": GENERIC_RGBD_ROUTE_CAPABILITY,
        "provider_id": provider_id,
        "provider_instance_id": provider_instance_id,
        "boot_id": boot_id,
        "available": available,
        "hardware_specific": False,
        "selection": {
            "role": "PRIMARY",
            "generic_route_preferred_when_compatible": True,
            "coexists_with_generic_route": True,
            "explicit_provider_id": provider_id,
        },
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "mapping_name": mapping_name,
            "layout_version": 2,
            "consumer_library": "orbbec_femto_provider.shared_memory_access",
            "reference_validation": "MAPPING_SLOT_GENERATION_AND_BOOT_ID",
            "adapter_scope": "PROVIDER_SUPPLIED",
            "provider_specific_layout": True,
            "buffer_reference_schema": "physical_agent.buffer_ref",
            "payload_location": "SHARED_MEMORY_ONLY",
            "large_payloads_on_fabric": False,
        },
        "products": {
            "channels": channels,
            "synchronized_bundle": {
                "stream": "camera.rgbd.bundle",
                "schema": "physical_agent.synchronized_buffer_bundle",
                "channels_may_have_independent_grids": True,
            },
            "calibration": {
                "stream": "camera.calibration",
                "revision": calibration_revision,
            },
        },
        "payload_policy": {
            "fabric_role": "TIMESTAMPS_REFERENCES_AND_SMALL_METADATA_ONLY",
            "large_payload_transport": "SHARED_MEMORY",
            "fabric_may_contain": [
                "timestamps",
                "buffer_references",
                "channel_geometry",
                "alignment_metadata",
                "calibration_revision",
                "small_numeric_or_text_status",
            ],
        },
        "channel_model": {
            "same_resolution_not_required": True,
            "same_aspect_ratio_not_required": True,
            "same_boundary_not_required": True,
            "per_channel_intrinsics_required_when_used_for_3d": True,
        },
        "alignments": [alignment],
        "synchronization": {
            "bundle_stream": "camera.rgbd.bundle",
            "policy": "TIMESTAMP_DELTA_WITH_PROVIDER_DECLARED_THRESHOLD",
            "alignment_does_not_imply_identical_capture_timestamp": True,
        },
    }


def build_direct_rgbd_route(
    *,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
    mapping_name: str,
    calibration_revision: str | None,
    rgb_ready: bool,
    depth_ready: bool,
) -> dict[str, Any]:
    available = bool(rgb_ready and depth_ready)
    return {
        "route_id": DIRECT_RGBD_ROUTE_ID,
        "capability": DIRECT_RGBD_ROUTE_CAPABILITY,
        "provider_id": provider_id,
        "provider_instance_id": provider_instance_id,
        "boot_id": boot_id,
        "available": available,
        "hardware_specific": True,
        "selection": {
            "role": "COMPATIBILITY_FALLBACK",
            "generic_route_preferred_when_compatible": True,
            "coexists_with_generic_route": True,
            "explicit_provider_id": provider_id,
        },
        "transport": {
            "kind": "WINDOWS_NAMED_SHARED_MEMORY",
            "mapping_name": mapping_name,
            "layout_version": 2,
            "consumer_library": "orbbec_femto_provider.shared_memory_access",
            "reference_validation": "MAPPING_SLOT_GENERATION_AND_BOOT_ID",
            "adapter_scope": "PROVIDER_SPECIFIC",
            "provider_specific_layout": True,
            "buffer_reference_schema": "physical_agent.buffer_ref",
            "payload_location": "SHARED_MEMORY_ONLY",
            "large_payloads_on_fabric": False,
        },
        "products": {
            "rgb": {
                "stream": "camera.rgb.frame_ref",
                "coordinate_frame": "femto_bolt_color_optical_frame",
                "coordinate_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
                "coordinate_axis_names": dict(
                    CAMERA_SYSTEM_AXIS_NAMES
                ),
            },
            "depth": {
                "stream": "camera.depth.frame_ref",
                "coordinate_frame": "femto_bolt_depth_optical_frame",
                "coordinate_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
                "coordinate_axis_names": dict(
                    CAMERA_SYSTEM_AXIS_NAMES
                ),
                "units": "millimeters",
            },
            "synchronized_bundle": {
                "stream": "camera.rgbd.bundle",
                "schema": "physical_agent.synchronized_buffer_bundle",
            },
            "calibration": {
                "stream": "camera.calibration",
                "revision": calibration_revision,
            },
        },
    }


def build_rgbd_route_set(
    direct_route: dict[str, Any],
    generic_route: dict[str, Any],
) -> dict[str, Any]:
    """Publish preferred and fallback routes atomically on one Fabric stream."""

    return {
        "route_set_schema_version": 1,
        "preferred_route_id": str(generic_route["route_id"]),
        "compatibility_fallback_route_ids": [str(direct_route["route_id"])],
        "selection_policy": "GENERIC_WHEN_COMPATIBLE_ELSE_EXPLICIT_PROVIDER_FALLBACK",
        "routes": [
            copy.deepcopy(direct_route),
            copy.deepcopy(generic_route),
        ],
        "payload_policy": {
            "fabric_role": "TIMESTAMPS_REFERENCES_AND_SMALL_METADATA_ONLY",
            "large_payload_transport": "SHARED_MEMORY",
            "large_payloads_on_fabric": False,
        },
    }
