from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw

from orbbec_femto_provider.shared_memory_access import (
    STREAM_ALIGNED_DEPTH,
    STREAM_COLOR,
    STREAM_DEPTH,
    BufferRef,
    CameraSharedMemory,
)

from .fabric_client import FabricClient
from .manager_client import ManagerClient
from .phase4_policy import Phase4Policy, report_operation_progress
from .rgb_capture import RgbCapture
from .route_resolver import (
    DIRECT_RGBD_ROUTE_CAPABILITY,
    GENERIC_RGBD_ROUTE_CAPABILITY,
    routes_from_observation,
    select_rgbd_route,
)
from .vlm_router import VisionLanguageRouter


@dataclass(frozen=True)
class RgbdEvidence:
    composite_bytes: bytes
    composite_mime_type: str
    composite_path: Path
    rgb_path: Path
    aligned_depth_path: Path
    bundle_observation: dict[str, Any]
    route: dict[str, Any]
    geometry: dict[str, Any]
    timing: dict[str, Any]
    numeric_quality: dict[str, Any]


def _ref_timestamp(reference: dict[str, Any]) -> int:
    for field in (
        "global_timestamp_us",
        "system_timestamp_us",
        "device_timestamp_us",
    ):
        value = int(reference.get(field) or 0)
        if value > 0:
            return value
    return 0


def _grid(reference: dict[str, Any]) -> tuple[int, int]:
    return int(reference["height"]), int(reference["width"])


def summarize_bundle_cadence(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(observations) < 2:
        raise ValueError("RGB-D cadence analysis requires at least two observations")

    channels = {
        "rgb": [],
        "native_depth": [],
        "aligned_depth": [],
    }
    synchronized_samples = 0
    maximum_delta_us = 0
    observed_rgb_depth_deltas_us: list[int] = []
    observed_rgb_aligned_deltas_us: list[int] = []

    for observation in observations:
        bundle = observation.get("data")
        if not isinstance(bundle, dict):
            raise RuntimeError("camera.rgbd.bundle has no data object")
        references = {
            "rgb": bundle.get("rgb"),
            "native_depth": bundle.get("depth"),
            "aligned_depth": bundle.get("depth_aligned_to_rgb"),
        }
        if not all(isinstance(reference, dict) for reference in references.values()):
            raise RuntimeError("RGB-D cadence sample is missing a required channel")
        if bool(bundle.get("synchronized")):
            synchronized_samples += 1
        sample_maximum_delta_us = int(bundle.get("max_delta_us") or 0)
        if sample_maximum_delta_us > 0:
            maximum_delta_us = (
                sample_maximum_delta_us
                if maximum_delta_us <= 0
                else min(maximum_delta_us, sample_maximum_delta_us)
            )
        rgb_timestamp_us = _ref_timestamp(references["rgb"])
        native_depth_timestamp_us = _ref_timestamp(references["native_depth"])
        aligned_depth_timestamp_us = _ref_timestamp(references["aligned_depth"])
        observed_rgb_depth_deltas_us.append(
            native_depth_timestamp_us - rgb_timestamp_us
        )
        observed_rgb_aligned_deltas_us.append(
            aligned_depth_timestamp_us - rgb_timestamp_us
        )
        for name, reference in references.items():
            channels[name].append(
                {
                    "frame_number": int(reference.get("frame_number") or 0),
                    "timestamp_us": _ref_timestamp(reference),
                    "grid": [
                        int(reference.get("height") or 0),
                        int(reference.get("width") or 0),
                    ],
                    "format_name": str(reference.get("format_name") or ""),
                }
            )

    channel_results: dict[str, Any] = {}
    blockers: list[str] = []
    for name, samples in channels.items():
        frame_numbers = [int(sample["frame_number"]) for sample in samples]
        timestamps_us = [int(sample["timestamp_us"]) for sample in samples]
        unique_frames = list(dict.fromkeys(frame_numbers))
        unique_timestamps = list(dict.fromkeys(timestamps_us))
        frame_span = frame_numbers[-1] - frame_numbers[0]
        timestamp_span_us = timestamps_us[-1] - timestamps_us[0]
        estimated_hz = (
            float(frame_span * 1_000_000.0 / timestamp_span_us)
            if frame_span > 0 and timestamp_span_us > 0
            else None
        )
        advanced = len(unique_frames) > 1 and len(unique_timestamps) > 1
        if not advanced:
            blockers.append(f"{name.upper()}_DID_NOT_ADVANCE")
        channel_results[name] = {
            "advanced": advanced,
            "first_frame_number": frame_numbers[0],
            "last_frame_number": frame_numbers[-1],
            "unique_frame_count": len(unique_frames),
            "first_timestamp_us": timestamps_us[0],
            "last_timestamp_us": timestamps_us[-1],
            "unique_timestamp_count": len(unique_timestamps),
            "estimated_hz": estimated_hz,
            "observed_grids": list(
                dict.fromkeys(tuple(sample["grid"]) for sample in samples)
            ),
            "observed_formats": list(
                dict.fromkeys(str(sample["format_name"]) for sample in samples)
            ),
        }

    if synchronized_samples != len(observations):
        blockers.append("NOT_ALL_CADENCE_SAMPLES_SYNCHRONIZED")
    maximum_rgb_depth_delta_us = max(
        abs(value) for value in observed_rgb_depth_deltas_us
    )
    maximum_rgb_aligned_delta_us = max(
        abs(value) for value in observed_rgb_aligned_deltas_us
    )
    if maximum_delta_us > 0:
        if maximum_rgb_depth_delta_us > maximum_delta_us:
            blockers.append("CADENCE_RGB_NATIVE_DEPTH_DELTA_EXCEEDED")
        if maximum_rgb_aligned_delta_us > maximum_delta_us:
            blockers.append("CADENCE_RGB_ALIGNED_DEPTH_DELTA_EXCEEDED")

    return {
        "status": "PASS" if not blockers else "FAIL",
        "motion_usable": not blockers,
        "sample_count": len(observations),
        "synchronized_sample_count": synchronized_samples,
        "frame_rate_equality_required": False,
        "per_channel": channel_results,
        "maximum_allowed_delta_us": maximum_delta_us,
        "maximum_observed_rgb_native_depth_delta_us": (
            maximum_rgb_depth_delta_us
        ),
        "maximum_observed_rgb_aligned_depth_delta_us": (
            maximum_rgb_aligned_delta_us
        ),
        "blockers": blockers,
    }


def decode_depth_m(payload: bytes, reference: dict[str, Any]) -> np.ndarray:
    format_name = str(reference.get("format_name") or "").upper()
    if format_name not in {"Y16", "DEPTH16", "Z16"}:
        raise RuntimeError(
            f"unsupported registered-depth format: {format_name or 'unknown'}"
        )
    width = int(reference["width"])
    height = int(reference["height"])
    stride_bytes = int(reference["stride_bytes"])
    if min(width, height, stride_bytes) <= 0 or stride_bytes % 2 != 0:
        raise RuntimeError("registered-depth grid or stride is invalid")
    required_bytes = height * stride_bytes
    if len(payload) < required_bytes:
        raise RuntimeError(
            "registered-depth payload is shorter than its declared stride"
        )
    stride_values = stride_bytes // 2
    values = np.frombuffer(
        payload,
        dtype="<u2",
        count=height * stride_values,
    ).reshape(height, stride_values)[:, :width]
    scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
    if scale_mm <= 0.0:
        scale_mm = 1.0
    return values.astype(np.float32) * (scale_mm / 1000.0)


def valid_depth_boundary(depth_m: np.ndarray) -> dict[str, Any] | None:
    valid = np.isfinite(depth_m) & (depth_m > 0.0)
    if not np.any(valid):
        return None
    rows, columns = np.nonzero(valid)
    x0 = int(columns.min())
    x1 = int(columns.max()) + 1
    y0 = int(rows.min())
    y1 = int(rows.max()) + 1
    return {
        "x": x0,
        "y": y0,
        "width": x1 - x0,
        "height": y1 - y0,
    }


def _boundary_iou(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> float | None:
    if left is None or right is None:
        return None
    left_x0 = int(left["x"])
    left_y0 = int(left["y"])
    left_x1 = left_x0 + int(left["width"])
    left_y1 = left_y0 + int(left["height"])
    right_x0 = int(right["x"])
    right_y0 = int(right["y"])
    right_x1 = right_x0 + int(right["width"])
    right_y1 = right_y0 + int(right["height"])
    intersection_width = max(0, min(left_x1, right_x1) - max(left_x0, right_x0))
    intersection_height = max(0, min(left_y1, right_y1) - max(left_y0, right_y0))
    intersection = intersection_width * intersection_height
    union = (
        int(left["width"]) * int(left["height"])
        + int(right["width"]) * int(right["height"])
        - intersection
    )
    return float(intersection / union) if union > 0 else 0.0


def _render_depth(depth_m: np.ndarray) -> np.ndarray:
    valid = np.isfinite(depth_m) & (depth_m > 0.0)
    rendered = np.zeros(depth_m.shape, dtype=np.uint8)
    if np.any(valid):
        low = float(np.percentile(depth_m[valid], 2))
        high = float(np.percentile(depth_m[valid], 98))
        if high <= low:
            high = low + 0.001
        scaled = np.clip((high - depth_m) * 255.0 / (high - low), 0, 255)
        rendered[valid] = scaled[valid].astype(np.uint8)
    color = cv2.applyColorMap(rendered, cv2.COLORMAP_TURBO)
    color[~valid] = 0
    return cv2.cvtColor(color, cv2.COLOR_BGR2RGB)


def _edge_alignment_score(
    rgb: np.ndarray,
    depth_m: np.ndarray,
) -> dict[str, Any]:
    if depth_m.shape != rgb.shape[:2]:
        return {
            "score": None,
            "reason": "REGISTERED_DEPTH_GRID_DOES_NOT_MATCH_RGB_GRID",
        }
    grayscale = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    rgb_edges = cv2.Canny(grayscale, 60, 140) > 0
    valid = np.isfinite(depth_m) & (depth_m > 0.0)
    depth_filled = depth_m.copy()
    if np.any(valid):
        depth_filled[~valid] = float(np.median(depth_m[valid]))
    depth_normalized = cv2.normalize(
        depth_filled,
        None,
        0,
        255,
        cv2.NORM_MINMAX,
    ).astype(np.uint8)
    depth_edges = cv2.Canny(depth_normalized, 20, 60) > 0
    depth_edges &= valid
    rgb_dilated = cv2.dilate(
        rgb_edges.astype(np.uint8),
        np.ones((5, 5), dtype=np.uint8),
    ) > 0
    depth_count = int(np.count_nonzero(depth_edges))
    overlap = int(np.count_nonzero(depth_edges & rgb_dilated))
    return {
        "score": float(overlap / depth_count) if depth_count else None,
        "rgb_edge_pixels": int(np.count_nonzero(rgb_edges)),
        "depth_edge_pixels": depth_count,
        "overlap_pixels": overlap,
        "reason": "DEPTH_EDGES_NEAR_RGB_EDGES",
    }


def build_alignment_composite(
    rgb: np.ndarray,
    depth_m: np.ndarray,
    *,
    observed_boundary: dict[str, Any] | None,
    maximum_panel_width: int = 800,
) -> tuple[bytes, dict[str, Any]]:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("RGB image must have shape HxWx3")
    rgb_height, rgb_width = rgb.shape[:2]
    if depth_m.shape != (rgb_height, rgb_width):
        display_depth = cv2.resize(
            depth_m,
            (rgb_width, rgb_height),
            interpolation=cv2.INTER_NEAREST,
        )
        depth_was_resized = True
    else:
        display_depth = depth_m
        depth_was_resized = False

    scale = min(1.0, maximum_panel_width / float(rgb_width))
    panel_width = max(1, int(round(rgb_width * scale)))
    panel_height = max(1, int(round(rgb_height * scale)))
    rgb_panel = cv2.resize(
        rgb,
        (panel_width, panel_height),
        interpolation=cv2.INTER_AREA,
    )
    depth_panel = cv2.resize(
        _render_depth(display_depth),
        (panel_width, panel_height),
        interpolation=cv2.INTER_NEAREST,
    )

    overlay = rgb_panel.astype(np.float32) * 0.62 + depth_panel.astype(
        np.float32
    ) * 0.38
    overlay_panel = np.clip(overlay, 0, 255).astype(np.uint8)
    label_height = 46
    canvas = Image.new(
        "RGB",
        (panel_width * 3, panel_height + label_height),
        (10, 10, 10),
    )
    canvas.paste(Image.fromarray(rgb_panel), (0, label_height))
    canvas.paste(Image.fromarray(depth_panel), (panel_width, label_height))
    canvas.paste(Image.fromarray(overlay_panel), (panel_width * 2, label_height))
    draw = ImageDraw.Draw(canvas)
    draw.text((12, 14), f"RGB {rgb_width}x{rgb_height}", fill=(245, 245, 245))
    draw.text(
        (panel_width + 12, 14),
        f"REGISTERED DEPTH {depth_m.shape[1]}x{depth_m.shape[0]}",
        fill=(245, 245, 245),
    )
    draw.text(
        (panel_width * 2 + 12, 14),
        "RGB + DEPTH OVERLAY",
        fill=(245, 245, 245),
    )
    if observed_boundary is not None:
        boundary_scale_x = panel_width / float(depth_m.shape[1])
        boundary_scale_y = panel_height / float(depth_m.shape[0])
        x0 = int(round(int(observed_boundary["x"]) * boundary_scale_x))
        y0 = int(round(int(observed_boundary["y"]) * boundary_scale_y))
        x1 = int(
            round(
                (int(observed_boundary["x"]) + int(observed_boundary["width"]))
                * boundary_scale_x
            )
        )
        y1 = int(
            round(
                (int(observed_boundary["y"]) + int(observed_boundary["height"]))
                * boundary_scale_y
            )
        )
        for panel_index in (1, 2):
            offset_x = panel_index * panel_width
            draw.rectangle(
                (
                    offset_x + x0,
                    label_height + y0,
                    offset_x + x1,
                    label_height + y1,
                ),
                outline=(255, 255, 255),
                width=3,
            )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=91)
    return output.getvalue(), {
        "panel_width": panel_width,
        "panel_height": panel_height,
        "depth_resized_for_display": depth_was_resized,
        "composite_layout": ["RGB", "REGISTERED_DEPTH", "OVERLAY"],
        "boundary_outline": "WHITE",
    }


def parse_alignment_vlm_result(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match is None:
            raise RuntimeError("VLM alignment review did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("VLM alignment review must be a JSON object")
    required_boolean = (
        "rgb_content_visible",
        "registered_depth_content_visible",
        "same_scene",
        "boundary_consistent",
        "major_misalignment",
    )
    for field in required_boolean:
        if not isinstance(value.get(field), bool):
            raise RuntimeError(f"VLM alignment review field {field} must be boolean")
    quality = str(value.get("alignment_quality") or "").upper()
    confidence = str(value.get("confidence") or "").upper()
    if quality not in {"PASS", "WARN", "FAIL"}:
        raise RuntimeError("VLM alignment_quality must be PASS, WARN, or FAIL")
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        raise RuntimeError("VLM confidence must be LOW, MEDIUM, or HIGH")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("VLM alignment review must include a reason")
    return {
        **value,
        "alignment_quality": quality,
        "confidence": confidence,
        "reason": reason.strip(),
    }


class RgbdEvidenceCapture:
    def __init__(
        self,
        fabric: FabricClient,
        screenshot_dir: Path,
        *,
        policy: Phase4Policy,
    ):
        self.fabric = fabric
        self.screenshot_dir = screenshot_dir
        self.policy = policy

    async def capture_latest(
        self,
        *,
        provider_id: str | None,
        binding_id: str | None,
    ) -> RgbdEvidence:
        report_operation_progress("READ_RGBD_ROUTE_SET")
        route_observation = await self.fabric.latest_optional(
            "camera.rgbd.data_routes"
        )
        raw_routes = routes_from_observation(route_observation)
        selection = select_rgbd_route(raw_routes, provider_id=provider_id)
        if selection is None:
            raise RuntimeError("no compatible RGB-D shared-memory route is available")
        if (
            self.policy.generic_rgbd_route == "ENFORCED"
            and selection.capability != GENERIC_RGBD_ROUTE_CAPABILITY
        ):
            raise RuntimeError(
                "generic RGB-D route enforcement rejected the direct provider route"
            )
        selected_route = next(
            (
                route
                for route in raw_routes
                if str(route.get("route_id")) == selection.route_id
            ),
            {},
        )
        cadence = await self.sample_cadence(provider_id=provider_id)

        reader = self._open_route_reader(selected_route)
        try:
            (
                observation,
                bundle,
                rgb_ref,
                depth_ref,
                aligned_ref,
                rgb_raw,
                aligned_raw,
                reference_source,
            ) = await self._read_latest_payloads(
                provider_id=provider_id,
                reader=reader,
            )
        finally:
            reader.close()

        report_operation_progress("DECODE_RGBD_EVIDENCE")
        rgb_jpeg, _ = RgbCapture._normalize_to_jpeg(rgb_raw, rgb_ref)
        rgb_image = np.asarray(Image.open(io.BytesIO(rgb_jpeg)).convert("RGB"))
        aligned_depth_m = decode_depth_m(aligned_raw, aligned_ref)
        observed_boundary = valid_depth_boundary(aligned_depth_m)
        if observed_boundary is None:
            raise RuntimeError("registered depth contains no valid samples")

        route_channels = (
            (selected_route.get("products") or {}).get("channels") or {}
        )
        route_registered = route_channels.get("depth_registered_to_rgb") or {}
        route_valid_region = route_registered.get("valid_region")
        rgb_grid = _grid(rgb_ref)
        native_depth_grid = _grid(depth_ref)
        aligned_depth_grid = _grid(aligned_ref)
        rgb_timestamp = _ref_timestamp(rgb_ref)
        depth_timestamp = _ref_timestamp(depth_ref)
        aligned_timestamp = _ref_timestamp(aligned_ref)
        synchronized = bool(bundle.get("synchronized"))
        maximum_delta_us = int(bundle.get("max_delta_us") or 0)
        rgb_depth_delta_us = int(bundle.get("timestamp_delta_us") or 0)
        rgb_aligned_delta_us = aligned_timestamp - rgb_timestamp
        valid_fraction = float(
            np.count_nonzero(aligned_depth_m > 0.0)
            / aligned_depth_m.size
        )
        edge_alignment = _edge_alignment_score(
            rgb_image,
            aligned_depth_m,
        )
        grid_matches = aligned_depth_grid == rgb_grid
        route_grid = (
            int(
                ((route_registered.get("native_grid") or {}).get("height"))
                or 0
            ),
            int(
                ((route_registered.get("native_grid") or {}).get("width"))
                or 0
            ),
        )
        route_grid_matches = route_grid == aligned_depth_grid
        boundary_iou = _boundary_iou(
            observed_boundary,
            (
                route_valid_region
                if isinstance(route_valid_region, dict)
                else None
            ),
        )
        blockers = []
        if not synchronized:
            blockers.append("PROVIDER_BUNDLE_NOT_SYNCHRONIZED")
        if maximum_delta_us and abs(rgb_depth_delta_us) > maximum_delta_us:
            blockers.append("RGB_NATIVE_DEPTH_TIMESTAMP_DELTA_EXCEEDED")
        if maximum_delta_us and abs(rgb_aligned_delta_us) > maximum_delta_us:
            blockers.append("RGB_ALIGNED_DEPTH_TIMESTAMP_DELTA_EXCEEDED")
        if not grid_matches:
            blockers.append("ALIGNED_DEPTH_GRID_DOES_NOT_MATCH_RGB")
        if (
            selection.capability == GENERIC_RGBD_ROUTE_CAPABILITY
            and not route_grid_matches
        ):
            blockers.append("GENERIC_ROUTE_GRID_DOES_NOT_MATCH_PAYLOAD")
        if valid_fraction < 0.02:
            blockers.append("ALIGNED_DEPTH_VALID_FRACTION_TOO_LOW")
        blockers.extend(
            blocker
            for blocker in cadence["blockers"]
            if blocker not in blockers
        )

        composite_bytes, display = build_alignment_composite(
            rgb_image,
            aligned_depth_m,
            observed_boundary=observed_boundary,
        )
        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
        rgb_path = self.screenshot_dir / f"rgbd_qc_rgb_{stamp}.jpg"
        depth_path = self.screenshot_dir / f"rgbd_qc_depth_{stamp}.png"
        composite_path = (
            self.screenshot_dir / f"rgbd_qc_composite_{stamp}.jpg"
        )
        rgb_path.write_bytes(rgb_jpeg)
        depth_png = io.BytesIO()
        Image.fromarray(_render_depth(aligned_depth_m)).save(
            depth_png,
            format="PNG",
        )
        depth_path.write_bytes(depth_png.getvalue())
        composite_path.write_bytes(composite_bytes)

        return RgbdEvidence(
            composite_bytes=composite_bytes,
            composite_mime_type="image/jpeg",
            composite_path=composite_path,
            rgb_path=rgb_path,
            aligned_depth_path=depth_path,
            bundle_observation=observation,
            route={
                **selection.as_dict(),
                "binding_id": binding_id,
                "raw_route": selected_route,
            },
            geometry={
                "rgb_grid": list(rgb_grid),
                "native_depth_grid": list(native_depth_grid),
                "aligned_depth_grid": list(aligned_depth_grid),
                "route_registered_depth_grid": list(route_grid),
                "observed_valid_boundary": observed_boundary,
                "route_valid_region": route_valid_region,
                "boundary_iou": boundary_iou,
                "valid_fraction": valid_fraction,
                "display": display,
            },
            timing={
                "synchronized": synchronized,
                "maximum_delta_us": maximum_delta_us,
                "rgb_depth_delta_us": rgb_depth_delta_us,
                "rgb_aligned_depth_delta_us": rgb_aligned_delta_us,
                "rgb_frame_number": int(rgb_ref.get("frame_number") or 0),
                "native_depth_frame_number": int(
                    depth_ref.get("frame_number") or 0
                ),
                "aligned_depth_frame_number": int(
                    aligned_ref.get("frame_number") or 0
                ),
                "rgb_timestamp_us": rgb_timestamp,
                "native_depth_timestamp_us": depth_timestamp,
                "aligned_depth_timestamp_us": aligned_timestamp,
                "reference_source": reference_source,
                "cadence": cadence,
            },
            numeric_quality={
                "status": "PASS" if not blockers else "FAIL",
                "motion_usable": not blockers,
                "blockers": blockers,
                "edge_alignment": edge_alignment,
                "generic_route_enforcement": self.policy.generic_rgbd_route,
                "selected_route_capability": selection.capability,
                "direct_fallback_selected": (
                    selection.capability == DIRECT_RGBD_ROUTE_CAPABILITY
                ),
                "provider_latest_ref_fallback_used": (
                    reference_source
                    == "PROVIDER_SHARED_MEMORY_LATEST_REF_FALLBACK"
                ),
            },
        )

    @staticmethod
    def _open_route_reader(selected_route: dict[str, Any]) -> CameraSharedMemory:
        transport = selected_route.get("transport")
        mapping_name = (
            str(transport.get("mapping_name") or "")
            if isinstance(transport, dict)
            else ""
        )
        if not mapping_name:
            raise RuntimeError("selected RGB-D route has no shared-memory mapping")
        return CameraSharedMemory(mapping_name).open()

    async def _read_latest_payloads(
        self,
        *,
        provider_id: str | None,
        reader: CameraSharedMemory,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        bytes,
        bytes,
        str,
    ]:
        last_error: Exception | None = None
        observation: dict[str, Any] | None = None
        bundle: dict[str, Any] | None = None
        for _ in range(3):
            report_operation_progress("READ_SYNCHRONIZED_RGBD_BUNDLE")
            observation = await self.fabric.latest("camera.rgbd.bundle")
            observed_provider_id = str(observation.get("provider_id") or "")
            if provider_id and observed_provider_id != provider_id:
                raise RuntimeError(
                    "RGB-D binding/provider mismatch: "
                    f"expected {provider_id}, observed "
                    f"{observed_provider_id or 'unknown'}"
                )
            bundle = observation.get("data")
            if not isinstance(bundle, dict):
                raise RuntimeError("camera.rgbd.bundle has no data object")
            rgb_ref = bundle.get("rgb")
            depth_ref = bundle.get("depth")
            aligned_ref = bundle.get("depth_aligned_to_rgb")
            if not all(isinstance(ref, dict) for ref in (rgb_ref, depth_ref)):
                raise RuntimeError("RGB-D bundle is missing RGB or native depth")
            if not isinstance(aligned_ref, dict):
                raise RuntimeError(
                    "RGB-D alignment check requires depth registered to RGB"
                )
            reference_mappings = {
                str(ref.get("mapping_name") or "")
                for ref in (rgb_ref, depth_ref, aligned_ref)
            }
            if reference_mappings != {reader.mapping_name}:
                raise RuntimeError(
                    "RGB-D route/bundle shared-memory mapping mismatch: "
                    f"route={reader.mapping_name!r}, "
                    f"bundle={sorted(reference_mappings)!r}"
                )
            try:
                # Copy the largest payload first, then the compressed color frame.
                # Reusing the pre-opened mapping avoids spending the finite two-slot
                # retention window remapping the camera region between channels.
                aligned_raw = reader.read_ref(aligned_ref)
                rgb_raw = reader.read_ref(rgb_ref)
            except Exception as error:
                last_error = error
                continue
            return (
                observation,
                bundle,
                rgb_ref,
                depth_ref,
                aligned_ref,
                rgb_raw,
                aligned_raw,
                "FABRIC_BUFFER_REFS",
            )
        if observation is None or bundle is None:
            raise RuntimeError("Fabric did not return an RGB-D bundle")

        report_operation_progress("READ_PROVIDER_LATEST_RGBD_REFS_FALLBACK")
        (
            rgb_ref,
            depth_ref,
            aligned_ref,
            rgb_raw,
            aligned_raw,
        ) = self._read_provider_latest_payloads(
            reader=reader,
            maximum_delta_us=int(bundle.get("max_delta_us") or 0),
        )
        refreshed_bundle = {
            **bundle,
            "rgb": rgb_ref,
            "depth": depth_ref,
            "depth_aligned_to_rgb": aligned_ref,
            "synchronized": True,
            "timestamp_delta_us": _ref_timestamp(depth_ref)
            - _ref_timestamp(rgb_ref),
            "consumer_reference_source": (
                "PROVIDER_SHARED_MEMORY_LATEST_REF_FALLBACK"
            ),
            "expired_fabric_reference_error": str(last_error),
        }
        refreshed_observation = {
            **observation,
            "data": refreshed_bundle,
        }
        return (
            refreshed_observation,
            refreshed_bundle,
            rgb_ref,
            depth_ref,
            aligned_ref,
            rgb_raw,
            aligned_raw,
            "PROVIDER_SHARED_MEMORY_LATEST_REF_FALLBACK",
        )

    @staticmethod
    def _read_provider_latest_payloads(
        *,
        reader: CameraSharedMemory,
        maximum_delta_us: int,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        bytes,
        bytes,
    ]:
        last_error: Exception | None = None
        for _ in range(4):
            try:
                aligned = reader.latest_ref(STREAM_ALIGNED_DEPTH)
                if aligned is None:
                    raise RuntimeError(
                        "provider shared memory has no aligned-depth frame"
                    )
                aligned_raw = reader.read_ref(aligned)

                rgb = reader.latest_ref(STREAM_COLOR)
                if rgb is None:
                    raise RuntimeError(
                        "provider shared memory has no RGB frame"
                    )
                rgb_raw = reader.read_ref(rgb)

                depth = reader.latest_ref(STREAM_DEPTH)
                if depth is None:
                    raise RuntimeError(
                        "provider shared memory has no native-depth frame"
                    )
            except RuntimeError as error:
                last_error = error
                continue

            rgb_ref = RgbdEvidenceCapture._reference_dict(rgb)
            depth_ref = RgbdEvidenceCapture._reference_dict(depth)
            aligned_ref = RgbdEvidenceCapture._reference_dict(aligned)
            rgb_timestamp_us = _ref_timestamp(rgb_ref)
            depth_timestamp_us = _ref_timestamp(depth_ref)
            aligned_timestamp_us = _ref_timestamp(aligned_ref)
            if min(
                rgb_timestamp_us,
                depth_timestamp_us,
                aligned_timestamp_us,
            ) <= 0:
                last_error = RuntimeError(
                    "provider latest references have no usable timestamps"
                )
                continue
            if maximum_delta_us > 0 and (
                abs(depth_timestamp_us - rgb_timestamp_us) > maximum_delta_us
                or abs(aligned_timestamp_us - rgb_timestamp_us)
                > maximum_delta_us
            ):
                last_error = RuntimeError(
                    "provider latest RGB-D references exceed the declared "
                    "synchronization threshold"
                )
                continue
            return (
                rgb_ref,
                depth_ref,
                aligned_ref,
                rgb_raw,
                aligned_raw,
            )
        raise RuntimeError(
            "provider-specific latest-reference fallback could not obtain a "
            f"fresh synchronized RGB-D sample: {last_error}"
        )

    @staticmethod
    def _reference_dict(reference: BufferRef | dict[str, Any]) -> dict[str, Any]:
        if isinstance(reference, BufferRef):
            return reference.to_dict()
        return dict(reference)

    async def sample_cadence(
        self,
        *,
        provider_id: str | None,
        sample_count: int = 6,
        interval_s: float = 0.12,
    ) -> dict[str, Any]:
        if sample_count < 2:
            raise ValueError("sample_count must be at least two")
        if not 0.01 <= interval_s <= 1.0:
            raise ValueError("interval_s must be between 0.01 and 1.0")
        observations: list[dict[str, Any]] = []
        for index in range(sample_count):
            report_operation_progress(
                f"SAMPLE_RGBD_CADENCE_{index + 1}_OF_{sample_count}"
            )
            observation = await self.fabric.latest("camera.rgbd.bundle")
            observed_provider_id = str(observation.get("provider_id") or "")
            if provider_id and observed_provider_id != provider_id:
                raise RuntimeError(
                    "RGB-D cadence provider mismatch: "
                    f"expected {provider_id}, observed "
                    f"{observed_provider_id or 'unknown'}"
                )
            observations.append(observation)
            if index + 1 < sample_count:
                await asyncio.sleep(interval_s)
        return summarize_bundle_cadence(observations)

class RgbdAlignmentValidationSkill:
    def __init__(
        self,
        capture: RgbdEvidenceCapture,
        router: VisionLanguageRouter,
        *,
        provider_id: str,
        manager: ManagerClient | None = None,
        policy: Phase4Policy | None = None,
    ):
        self.capture = capture
        self.router = router
        self.provider_id = provider_id
        self.manager = manager
        self.policy = policy or Phase4Policy.from_environment()
        self.last_result: dict[str, Any] | None = None
        self.last_binding: dict[str, Any] | None = None

    async def run(
        self,
        request: str,
        *,
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        binding = await self._bind_camera()
        self.last_binding = dict(binding)
        selected_provider_id = self._camera_provider_id(binding)
        evidence = await self.capture.capture_latest(
            provider_id=selected_provider_id,
            binding_id=(
                str(binding.get("binding_id"))
                if binding.get("binding_id") is not None
                else binding_id
            ),
        )
        binding = await self._revalidate_binding(binding)
        self.last_binding = dict(binding)
        binding_usable = self._binding_usable(binding)
        report_operation_progress("VLM_REVIEW_RGBD_COMPOSITE")
        inference = await self.router.generate(
            image_bytes=evidence.composite_bytes,
            mime_type=evidence.composite_mime_type,
            prompt=self._prompt(request, evidence),
        )
        vlm_review = parse_alignment_vlm_result(inference.text)
        vlm_usable = bool(
            vlm_review["rgb_content_visible"]
            and vlm_review["registered_depth_content_visible"]
            and vlm_review["same_scene"]
            and vlm_review["boundary_consistent"]
            and not vlm_review["major_misalignment"]
            and vlm_review["alignment_quality"] != "FAIL"
        )
        numeric_usable = bool(evidence.numeric_quality["motion_usable"])
        result = {
            "schema": "physical_agent.rgbd_alignment_validation",
            "schema_version": 1,
            "request": request,
            "motion_usable": bool(
                numeric_usable and vlm_usable and binding_usable
            ),
            "numeric_quality": evidence.numeric_quality,
            "vlm_review": vlm_review,
            "vlm_route": inference.as_dict(),
            "geometry": evidence.geometry,
            "timing": evidence.timing,
            "data_route": evidence.route,
            "capability_binding": binding,
            "artifacts": {
                "rgb": str(evidence.rgb_path),
                "registered_depth": str(evidence.aligned_depth_path),
                "composite": str(evidence.composite_path),
            },
            "image_review_required": True,
            "images_present_is_not_sufficient": True,
        }
        self.last_result = result
        return result

    async def capture_for_builtin_review(
        self,
        request: str,
        *,
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        """Capture exact RGB-D evidence without invoking an external VLM."""

        binding = await self._bind_camera()
        self.last_binding = dict(binding)
        selected_provider_id = self._camera_provider_id(binding)
        evidence = await self.capture.capture_latest(
            provider_id=selected_provider_id,
            binding_id=(
                str(binding.get("binding_id"))
                if binding.get("binding_id") is not None
                else binding_id
            ),
        )
        binding = await self._revalidate_binding(binding)
        self.last_binding = dict(binding)
        result = {
            "schema": (
                "physical_agent.rgbd_alignment_builtin_review_request"
            ),
            "schema_version": 1,
            "request": request,
            "review_state": "BUILTIN_MULTIMODAL_REVIEW_REQUIRED",
            "motion_usable": False,
            "numeric_quality": evidence.numeric_quality,
            "numeric_quality_passed": bool(
                evidence.numeric_quality["motion_usable"]
            ),
            "binding_usable": self._binding_usable(binding),
            "geometry": evidence.geometry,
            "timing": evidence.timing,
            "data_route": evidence.route,
            "capability_binding": binding,
            "artifacts": {
                "rgb": str(evidence.rgb_path),
                "registered_depth": str(
                    evidence.aligned_depth_path
                ),
                "composite": str(evidence.composite_path),
            },
            "composite_sha256": hashlib.sha256(
                evidence.composite_bytes
            ).hexdigest(),
            "image_review_required": True,
            "images_present_is_not_sufficient": True,
            "external_vlm_called": False,
        }
        self.last_result = result
        return result

    async def _bind_camera(self) -> dict[str, Any]:
        if self.manager is None:
            if self.policy.binding == "ENFORCED":
                raise RuntimeError(
                    "binding enforcement requires an available Manager client"
                )
            return {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.provider_id,
                "reason": "manager client is not configured",
            }
        try:
            binding = await self.manager.bind_capabilities(
                [
                    "camera.rgb",
                    "camera.depth_aligned_to_rgb",
                    "camera.rgbd.bundle",
                ],
                fallback_provider_ids={
                    "camera.rgb": self.provider_id,
                    "camera.depth_aligned_to_rgb": self.provider_id,
                    "camera.rgbd.bundle": self.provider_id,
                },
                related_skill_id="verify-rgbd-alignment",
            )
            return await self._revalidate_binding(binding)
        except Exception as error:
            if self.policy.binding == "ENFORCED":
                raise RuntimeError(
                    f"binding enforcement rejected camera fallback: {error}"
                ) from error
            return {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.provider_id,
                "reason": f"advisory binding unavailable: {error}",
            }

    async def _revalidate_binding(
        self,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        binding_id = binding.get("binding_id")
        if (
            self.manager is None
            or not isinstance(binding_id, str)
            or not binding_id
        ):
            return binding
        return await self.manager.capability_binding(binding_id)

    def _binding_usable(self, binding: dict[str, Any]) -> bool:
        validity = str(binding.get("validity") or "")
        usable = validity in {"CURRENT", "FALLBACK_REQUIRES_ACTIVATION"}
        if self.policy.binding == "ENFORCED" and validity != "CURRENT":
            raise RuntimeError(
                "binding enforcement requires CURRENT camera binding, got "
                f"{validity or 'UNKNOWN'}"
            )
        return usable

    def _camera_provider_id(self, binding: dict[str, Any]) -> str:
        selections = binding.get("selections")
        if isinstance(selections, list):
            selected = {
                str(selection.get("provider_id"))
                for selection in selections
                if isinstance(selection, dict)
                and selection.get("provider_id")
            }
            if len(selected) > 1:
                raise RuntimeError(
                    "RGB-D binding selected multiple camera providers"
                )
            if selected:
                return next(iter(selected))
        return str(binding.get("provider_id") or self.provider_id)

    @staticmethod
    def _prompt(request: str, evidence: RgbdEvidence) -> str:
        return f"""
You are reviewing a labeled three-panel RGB-D diagnostic image:
left is the RGB frame, center is depth registered into RGB coordinates, and
right is a blended overlay. A white rectangle marks the observed nonzero-depth
boundary on the depth and overlay panels.

The camera can publish RGB, native depth, IR, and registered depth at different
resolutions, aspect ratios, boundaries, timestamps, and frame rates. Do not
assume that image presence means correct registration. Inspect actual scene
content. Check whether recognizable depth discontinuities correspond to RGB
object boundaries, whether the RGB and registered-depth panels depict the same
scene, whether crops or invalid borders are represented by the white boundary,
and whether there is an obvious shift, scale error, mirror, rotation, or stale
frame.

User purpose: {request}
Numeric timing: {json.dumps(evidence.timing, sort_keys=True)}
Numeric geometry: {json.dumps(evidence.geometry, sort_keys=True)}

Return only one JSON object with exactly these fields:
{{
  "rgb_content_visible": true,
  "registered_depth_content_visible": true,
  "same_scene": true,
  "boundary_consistent": true,
  "major_misalignment": false,
  "alignment_quality": "PASS",
  "confidence": "MEDIUM",
  "reason": "short evidence-based explanation"
}}
Use PASS, WARN, or FAIL for alignment_quality and LOW, MEDIUM, or HIGH for
confidence. If the panels are ambiguous, blank, stale-looking, or cannot support
an alignment judgment, do not return PASS.
""".strip()
