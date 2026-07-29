from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


GENERIC_RGBD_ROUTE_CAPABILITY = "camera.rgbd.route.generic_shared_memory"
DIRECT_RGBD_ROUTE_CAPABILITY = "camera.rgbd.route.direct_shared_memory"


@dataclass(frozen=True)
class DataRouteSelection:
    route_id: str
    capability: str
    provider_id: str
    provider_instance_id: str
    boot_id: str
    hardware_specific: bool
    selection_reason: str
    transport_kind: str
    consumer_library: str
    rejected_generic_route_issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rejected_generic_route_issues"] = list(
            self.rejected_generic_route_issues
        )
        return value


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_region_issues(
    channel_id: str,
    region: Any,
    *,
    width: int,
    height: int,
) -> list[str]:
    prefix = f"CHANNEL_{channel_id.upper()}"
    if not isinstance(region, dict):
        return [f"{prefix}_VALID_REGION_MISSING"]
    values = {
        key: region.get(key)
        for key in ("x", "y", "width", "height")
    }
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in values.values()
    ):
        return [f"{prefix}_VALID_REGION_INVALID"]
    x = int(values["x"])
    y = int(values["y"])
    region_width = int(values["width"])
    region_height = int(values["height"])
    if (
        x < 0
        or y < 0
        or region_width <= 0
        or region_height <= 0
        or x + region_width > width
        or y + region_height > height
    ):
        return [f"{prefix}_VALID_REGION_OUT_OF_BOUNDS"]
    return []


def generic_rgbd_route_issues(route: dict[str, Any]) -> tuple[str, ...]:
    """Validate the producer-neutral RGB-D descriptor, not its reader module."""

    issues: list[str] = []
    if str(route.get("capability") or "") != GENERIC_RGBD_ROUTE_CAPABILITY:
        return ("NOT_GENERIC_RGBD_ROUTE",)
    if bool(route.get("hardware_specific")):
        issues.append("GENERIC_ROUTE_MARKED_HARDWARE_SPECIFIC")

    transport = route.get("transport")
    if not isinstance(transport, dict):
        issues.append("TRANSPORT_MISSING")
    else:
        if str(transport.get("kind") or "") not in {
            "SHARED_MEMORY",
            "WINDOWS_NAMED_SHARED_MEMORY",
        }:
            issues.append("TRANSPORT_NOT_SHARED_MEMORY")
        if not str(transport.get("mapping_name") or ""):
            issues.append("TRANSPORT_MAPPING_NAME_MISSING")
        if (
            str(transport.get("buffer_reference_schema") or "")
            != "physical_agent.buffer_ref"
        ):
            issues.append("BUFFER_REFERENCE_SCHEMA_UNSUPPORTED")
        if str(transport.get("payload_location") or "") != "SHARED_MEMORY_ONLY":
            issues.append("LARGE_PAYLOAD_LOCATION_INVALID")
        if bool(transport.get("large_payloads_on_fabric", True)):
            issues.append("LARGE_PAYLOADS_MUST_NOT_USE_FABRIC")
        if not str(transport.get("consumer_library") or ""):
            issues.append("CONSUMER_LIBRARY_MISSING")

    payload_policy = route.get("payload_policy")
    if not isinstance(payload_policy, dict):
        issues.append("PAYLOAD_POLICY_MISSING")
    else:
        if payload_policy.get("large_payloads_on_fabric") is True:
            issues.append("PAYLOAD_POLICY_ALLOWS_LARGE_FABRIC_DATA")
        if (
            str(payload_policy.get("large_payload_transport") or "")
            != "SHARED_MEMORY"
        ):
            issues.append("PAYLOAD_POLICY_TRANSPORT_INVALID")

    channel_model = route.get("channel_model")
    if not isinstance(channel_model, dict):
        issues.append("CHANNEL_MODEL_MISSING")
    else:
        for field in (
            "same_resolution_not_required",
            "same_aspect_ratio_not_required",
            "same_boundary_not_required",
            "per_channel_intrinsics_required_when_used_for_3d",
        ):
            if channel_model.get(field) is not True:
                issues.append(f"CHANNEL_MODEL_{field.upper()}_NOT_DECLARED")

    products = route.get("products")
    channels = (
        products.get("channels")
        if isinstance(products, dict)
        else None
    )
    product_calibration = (
        products.get("calibration")
        if isinstance(products, dict)
        else None
    )
    if (
        not isinstance(product_calibration, dict)
        or not str(product_calibration.get("stream") or "")
        or not str(product_calibration.get("revision") or "")
    ):
        issues.append("PRODUCT_CALIBRATION_REFERENCE_MISSING")
    required_channels = (
        "rgb",
        "infrared",
        "depth",
        "depth_registered_to_rgb",
    )
    channel_regions: dict[str, dict[str, Any]] = {}
    if not isinstance(channels, dict):
        issues.append("CHANNEL_PRODUCTS_MISSING")
    else:
        for channel_id in required_channels:
            channel = channels.get(channel_id)
            prefix = f"CHANNEL_{channel_id.upper()}"
            if not isinstance(channel, dict) or channel.get("available") is not True:
                issues.append(f"{prefix}_MISSING")
                continue
            grid = channel.get("native_grid")
            if not isinstance(grid, dict):
                issues.append(f"{prefix}_GRID_MISSING")
                continue
            width = grid.get("width")
            height = grid.get("height")
            stride = grid.get("stride_bytes")
            if not (
                _positive_integer(width)
                and _positive_integer(height)
                and _positive_integer(stride)
            ):
                issues.append(f"{prefix}_GRID_INVALID")
                continue
            if grid.get("independent_resolution") is not True:
                issues.append(f"{prefix}_INDEPENDENT_RESOLUTION_NOT_DECLARED")
            if grid.get("independent_aspect_ratio") is not True:
                issues.append(f"{prefix}_INDEPENDENT_ASPECT_RATIO_NOT_DECLARED")
            region = channel.get("valid_region")
            issues.extend(
                _valid_region_issues(
                    channel_id,
                    region,
                    width=int(width),
                    height=int(height),
                )
            )
            if isinstance(region, dict):
                channel_regions[channel_id] = region
            timestamp = channel.get("timestamp")
            if (
                not isinstance(timestamp, dict)
                or not isinstance(timestamp.get("selection_order"), list)
                or not timestamp["selection_order"]
            ):
                issues.append(f"{prefix}_TIMESTAMP_POLICY_MISSING")
            sample = channel.get("sample")
            if (
                not isinstance(sample, dict)
                or not str(sample.get("format_name") or "")
            ):
                issues.append(f"{prefix}_SAMPLE_FORMAT_MISSING")
            calibration = channel.get("calibration")
            if (
                not isinstance(calibration, dict)
                or not str(calibration.get("stream") or "")
                or not str(calibration.get("document_path") or "")
            ):
                issues.append(f"{prefix}_CALIBRATION_REFERENCE_MISSING")

    synchronization = route.get("synchronization")
    if (
        not isinstance(synchronization, dict)
        or not str(synchronization.get("bundle_stream") or "")
        or not str(synchronization.get("policy") or "")
    ):
        issues.append("SYNCHRONIZATION_POLICY_MISSING")

    alignments = route.get("alignments")
    alignment = next(
        (
            candidate
            for candidate in alignments
            if isinstance(candidate, dict)
            and candidate.get("output_channel") == "depth_registered_to_rgb"
        ),
        None,
    ) if isinstance(alignments, list) else None
    if not isinstance(alignment, dict):
        issues.append("REGISTERED_DEPTH_ALIGNMENT_MISSING")
    else:
        if not str(alignment.get("alignment_id") or ""):
            issues.append("REGISTERED_DEPTH_ALIGNMENT_ID_MISSING")
        alignment_revision = str(alignment.get("calibration_revision") or "")
        if not alignment_revision:
            issues.append(
                "REGISTERED_DEPTH_ALIGNMENT_CALIBRATION_REVISION_MISSING"
            )
        product_revision = str(
            (
                product_calibration.get("revision")
                if isinstance(product_calibration, dict)
                else None
            )
            or ""
        )
        if (
            alignment_revision
            and product_revision
            and alignment_revision != product_revision
        ):
            issues.append(
                "REGISTERED_DEPTH_ALIGNMENT_CALIBRATION_REVISION_MISMATCH"
            )
        expected = {
            "source_channel": "depth",
            "target_channel": "rgb",
            "output_grid": "TARGET_CHANNEL_NATIVE_GRID",
        }
        for field, value in expected.items():
            if str(alignment.get(field) or "") != value:
                issues.append(
                    f"REGISTERED_DEPTH_ALIGNMENT_{field.upper()}_INVALID"
                )
        for field in (
            "allows_source_target_resolution_mismatch",
            "allows_source_target_aspect_ratio_mismatch",
            "allows_non_overlapping_boundaries",
        ):
            if alignment.get(field) is not True:
                issues.append(
                    f"REGISTERED_DEPTH_ALIGNMENT_{field.upper()}_NOT_DECLARED"
                )
        boundary_model = alignment.get("boundary_model")
        output_region = (
            boundary_model.get("output_valid_region")
            if isinstance(boundary_model, dict)
            else None
        )
        registered_region = channel_regions.get("depth_registered_to_rgb")
        if (
            not isinstance(output_region, dict)
            or not isinstance(registered_region, dict)
            or any(
                output_region.get(field) != registered_region.get(field)
                for field in ("x", "y", "width", "height")
            )
        ):
            issues.append("REGISTERED_DEPTH_ALIGNMENT_BOUNDARY_MISMATCH")

    return tuple(dict.fromkeys(issues))


def select_rgbd_route(
    routes: Iterable[dict[str, Any]],
    *,
    provider_id: str | None,
    supported_capabilities: set[str] | None = None,
) -> DataRouteSelection | None:
    """Prefer a compatible generic route and retain the branded fallback."""

    supported = supported_capabilities or {
        GENERIC_RGBD_ROUTE_CAPABILITY,
        DIRECT_RGBD_ROUTE_CAPABILITY,
    }
    candidates: list[dict[str, Any]] = []
    rejected_generic_issues: list[str] = []
    for route in routes:
        if not isinstance(route, dict) or not bool(route.get("available")):
            continue
        if provider_id and str(route.get("provider_id")) != provider_id:
            continue
        if str(route.get("capability")) not in supported:
            continue
        if str(route.get("capability")) == GENERIC_RGBD_ROUTE_CAPABILITY:
            issues = generic_rgbd_route_issues(route)
            if issues:
                route_id = str(route.get("route_id") or "<missing-route-id>")
                rejected_generic_issues.extend(
                    f"{route_id}:{issue}" for issue in issues
                )
                continue
        candidates.append(route)
    if not candidates:
        return None

    def rank(route: dict[str, Any]) -> tuple[int, int, str]:
        capability = str(route.get("capability"))
        generic_rank = 0 if capability == GENERIC_RGBD_ROUTE_CAPABILITY else 1
        role_rank = (
            0
            if str((route.get("selection") or {}).get("role")) == "PRIMARY"
            else 1
        )
        return generic_rank, role_rank, str(route.get("route_id"))

    selected = sorted(candidates, key=rank)[0]
    capability = str(selected["capability"])
    transport = selected.get("transport") or {}
    reason = (
        "GENERIC_ROUTE_PREFERRED"
        if capability == GENERIC_RGBD_ROUTE_CAPABILITY
        else (
            "INVALID_GENERIC_ROUTE_EXPLICIT_PROVIDER_FALLBACK"
            if rejected_generic_issues
            else "EXPLICIT_PROVIDER_COMPATIBILITY_FALLBACK"
        )
    )
    return DataRouteSelection(
        route_id=str(selected["route_id"]),
        capability=capability,
        provider_id=str(selected["provider_id"]),
        provider_instance_id=str(selected["provider_instance_id"]),
        boot_id=str(selected["boot_id"]),
        hardware_specific=bool(selected.get("hardware_specific")),
        selection_reason=reason,
        transport_kind=str(transport.get("kind") or ""),
        consumer_library=str(transport.get("consumer_library") or ""),
        rejected_generic_route_issues=tuple(rejected_generic_issues),
    )


def routes_from_observation(observation: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not observation:
        return []
    data = observation.get("data")
    if isinstance(data, dict) and isinstance(data.get("routes"), list):
        return [route for route in data["routes"] if isinstance(route, dict)]
    return [data] if isinstance(data, dict) and data.get("route_id") else []
