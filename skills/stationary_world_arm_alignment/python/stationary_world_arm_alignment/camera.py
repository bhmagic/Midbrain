from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from orbbec_femto_provider.shared_memory_access import (
    STREAM_ALIGNED_DEPTH,
    STREAM_COLOR,
    BufferRef,
    CameraSharedMemory,
)

from .clients import FabricClient
from .math3d import deproject_pixel


WORLD_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)


@dataclass(frozen=True)
class RgbdFrame:
    rgb: np.ndarray
    depth_m: np.ndarray
    intrinsics: dict[str, Any]
    timestamp_us: int
    frame_number: int
    camera_frame: str
    session_epoch: str | None
    world_frame: str | None
    calibration_revision: str | None
    observations: dict[str, Any]


@dataclass(frozen=True)
class TipDepth:
    pixel_yx: tuple[int, int]
    depth_m: float
    camera_system_xyz_m: np.ndarray
    method: str
    valid_pixel_count: int
    cluster_span_m: float


class RgbdCapture:
    def __init__(self, fabric: FabricClient, camera_frame: str):
        self.fabric = fabric
        self.camera_frame = camera_frame

    async def capture(
        self,
        attempts: int = 8,
        retry_delay_s: float = 0.04,
        *,
        require_vio: bool = True,
    ) -> RgbdFrame:
        calibration_observation = await self.fabric.latest_optional("camera.calibration")
        route_observation = await self.fabric.latest_optional(
            "camera.rgbd.data_routes"
        )
        device_observation = await self.fabric.latest_optional(
            "camera.device_info"
        )
        if not calibration_observation:
            raise RuntimeError("camera calibration is unavailable")

        last_error: Exception | None = None
        transient_errors: list[str] = []
        reader: CameraSharedMemory | None = None
        reader_mapping_name: str | None = None
        try:
            for attempt in range(attempts):
                # Fetch the high-rate bundle last and copy it immediately. Reading
                # slower calibration and VIO observations after the bundle can
                # outlive the camera ring-buffer slot before the first shared-memory
                # read.
                bundle_observation = await self.fabric.latest_optional(
                    "camera.rgbd.bundle"
                )
                if not bundle_observation:
                    raise RuntimeError("RGB-D bundle is unavailable")
                bundle = bundle_observation.get("data") or {}
                coordinate_conventions = (
                    bundle.get("coordinate_conventions") or {}
                )
                if (
                    coordinate_conventions.get("rgb")
                    != CAMERA_OPTICAL_CONVENTION_ID
                    or coordinate_conventions.get("aligned_depth")
                    != CAMERA_OPTICAL_CONVENTION_ID
                ):
                    raise RuntimeError(
                        "RGB-D bundle does not declare native optical "
                        "X-right/Y-down/Z-forward coordinates"
                    )
                rgb_ref = bundle.get("rgb")
                depth_ref = bundle.get("depth_aligned_to_rgb")
                if not isinstance(rgb_ref, dict) or not isinstance(
                    depth_ref, dict
                ):
                    raise RuntimeError(
                        "RGB-D bundle has no aligned RGB and depth BufferRefs"
                    )
                mapping_name = str(rgb_ref.get("mapping_name") or "")
                if not mapping_name:
                    raise RuntimeError(
                        "RGB BufferRef has no shared-memory mapping name"
                    )
                if reader is None or reader_mapping_name != mapping_name:
                    if reader is not None:
                        reader.close()
                    reader = CameraSharedMemory(mapping_name).open()
                    reader_mapping_name = mapping_name

                copied_rgb_ref = dict(rgb_ref)
                copied_depth_ref = dict(depth_ref)
                buffer_ref_source = "FABRIC_SYNCHRONIZED_BUNDLE"
                try:
                    # Copy the larger depth plane first so it does not wait
                    # behind RGB decoding while its finite-retention slot is
                    # advancing.
                    depth = self._read_depth(reader, copied_depth_ref)
                    rgb = self._read_rgb(reader, copied_rgb_ref)
                except Exception as error:
                    if not self._is_transient_buffer_error(error):
                        raise
                    last_error = error
                    transient_errors.append(str(error))
                    try:
                        (
                            rgb,
                            depth,
                            copied_rgb_ref,
                            copied_depth_ref,
                        ) = self._copy_latest_synchronized_pair(
                            reader,
                            bundle=bundle,
                        )
                        buffer_ref_source = (
                            "LOCAL_MAPPING_SYNCHRONIZED_FALLBACK"
                        )
                    except Exception as fallback_error:
                        last_error = fallback_error
                        if attempt + 1 < attempts and retry_delay_s > 0:
                            await asyncio.sleep(retry_delay_s)
                        continue
                copied_at_us = time.time_ns() // 1000
            # Read the high-rate VIO state after the finite-retention RGB-D
            # payload has been copied. Fetching it before BufferRef retries can
            # associate a several-second-old pose with the eventual camera
            # frame even though VIO continued tracking throughout the copy.
                if require_vio:
                    pose_observation, vio_observation = await asyncio.gather(
                        self.fabric.latest_optional(
                            "localization.body.pose"
                        ),
                        self.fabric.latest_optional(
                            "localization.vio.status"
                        ),
                    )
                    if not pose_observation:
                        raise RuntimeError("VIO body pose is unavailable")
                else:
                    pose_observation = None
                    vio_observation = None
                calibration = calibration_observation.get("data") or {}
                calibration_conventions = (
                    calibration.get("coordinate_conventions") or {}
                )
                if (
                    calibration_conventions.get("color")
                    != CAMERA_OPTICAL_CONVENTION_ID
                ):
                    raise RuntimeError(
                        "camera calibration does not identify the color "
                        "optical coordinate convention"
                    )
                intrinsics = calibration.get("rgb_intrinsic") or {}
                if float(intrinsics.get("fx") or 0) <= 0:
                    raise RuntimeError("camera RGB intrinsics are invalid")
                pose = (pose_observation or {}).get("data") or {}
                vio = (vio_observation or {}).get("data") or {}
                epoch: str | None = None
                world: str | None = None
                if require_vio:
                    if (
                        pose.get("convention_id") != WORLD_CONVENTION_ID
                        or vio.get("convention_id") != WORLD_CONVENTION_ID
                    ):
                        raise RuntimeError(
                            "VIO pose/status do not declare the convention-V2 "
                            "Z-up world"
                        )
                    epoch = str(
                        pose.get("session_epoch")
                        or vio.get("session_epoch")
                        or ""
                    )
                    world = str(pose.get("world_frame") or "")
                    if not epoch or not world:
                        raise RuntimeError(
                            "VIO has not published a world frame and session "
                            "epoch"
                        )
                timestamp_us = self.reference_timestamp(copied_rgb_ref)
                if timestamp_us <= 0:
                    timestamp_us = int(
                        bundle_observation.get("observed_at_us") or 0
                    )
                return RgbdFrame(
                    rgb=rgb,
                    depth_m=depth,
                    intrinsics=intrinsics,
                    timestamp_us=timestamp_us,
                    frame_number=int(
                        copied_rgb_ref.get("frame_number") or -1
                    ),
                    camera_frame=self.camera_frame,
                    session_epoch=epoch,
                    world_frame=world,
                    calibration_revision=(
                        str(
                            calibration.get("calibration_revision")
                            or calibration_observation.get(
                                "calibration_revision"
                            )
                        )
                        if (
                            calibration.get("calibration_revision")
                            is not None
                            or calibration_observation.get(
                                "calibration_revision"
                            )
                            is not None
                        )
                        else None
                    ),
                    observations={
                        "bundle": self._effective_bundle_observation(
                            bundle_observation,
                            rgb_ref=copied_rgb_ref,
                            depth_ref=copied_depth_ref,
                            source=buffer_ref_source,
                        ),
                        "route": route_observation,
                        "device_info": device_observation,
                        "calibration": calibration_observation,
                        "body_pose": pose_observation,
                        "vio_status": vio_observation,
                        "capture": {
                            "copy_attempt": attempt + 1,
                            "transient_buffer_error_count": len(
                                transient_errors
                            ),
                            "vio_required": bool(require_vio),
                            "buffer_ref_source": buffer_ref_source,
                            "copied_at_us": copied_at_us,
                            "rgb_frame_number": int(
                                copied_rgb_ref.get("frame_number") or -1
                            ),
                            "aligned_depth_frame_number": int(
                                copied_depth_ref.get("frame_number") or -1
                            ),
                            "synchronization_delta_us": (
                                self.reference_timestamp(copied_depth_ref)
                                - self.reference_timestamp(copied_rgb_ref)
                            ),
                        },
                    },
                )
        finally:
            if reader is not None:
                reader.close()
        raise RuntimeError(
            "camera BufferRef remained unavailable after "
            f"{attempts} fresh-bundle attempts: {last_error}"
        )

    @classmethod
    def _effective_bundle_observation(
        cls,
        observation: dict[str, Any],
        *,
        rgb_ref: dict[str, Any],
        depth_ref: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        """Describe the exact copied pair used by downstream temporal gates."""

        if source == "FABRIC_SYNCHRONIZED_BUNDLE":
            return observation
        effective = dict(observation)
        data = dict(observation.get("data") or {})
        data["rgb"] = dict(rgb_ref)
        data["depth_aligned_to_rgb"] = dict(depth_ref)
        effective["data"] = data
        effective["observed_at_us"] = max(
            cls.reference_timestamp(rgb_ref),
            cls.reference_timestamp(depth_ref),
        )
        effective.pop("received_at", None)
        effective["capture_source"] = source
        effective["fabric_bundle_observed_at_us"] = int(
            observation.get("observed_at_us") or 0
        )
        return effective

    @classmethod
    def _copy_latest_synchronized_pair(
        cls,
        reader: CameraSharedMemory,
        *,
        bundle: dict[str, Any],
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any], dict[str, Any]]:
        """Copy the newest retained local RGB/aligned-depth pair."""

        reader.refresh()
        rgb_refs = reader.recent_refs(STREAM_COLOR, attempts=8)
        depth_refs = reader.recent_refs(STREAM_ALIGNED_DEPTH, attempts=8)
        maximum_delta_us = max(0, int(bundle.get("max_delta_us") or 0))
        candidates: list[tuple[int, int, BufferRef, BufferRef]] = []
        for rgb_ref in rgb_refs:
            rgb_timestamp = cls.reference_timestamp(rgb_ref.to_dict())
            if rgb_timestamp <= 0:
                continue
            for depth_ref in depth_refs:
                depth_timestamp = cls.reference_timestamp(
                    depth_ref.to_dict()
                )
                if depth_timestamp <= 0:
                    continue
                delta_us = depth_timestamp - rgb_timestamp
                if abs(delta_us) > maximum_delta_us:
                    continue
                candidates.append(
                    (
                        min(rgb_timestamp, depth_timestamp),
                        -abs(delta_us),
                        rgb_ref,
                        depth_ref,
                    )
                )
        if not candidates:
            raise RuntimeError(
                "no synchronized RGB/aligned-depth pair remains in the "
                "camera mapping"
            )
        candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
        last_error: Exception | None = None
        for _, _, rgb_ref, depth_ref in candidates:
            rgb_payload = rgb_ref.to_dict()
            depth_payload = depth_ref.to_dict()
            try:
                depth = cls._read_depth(reader, depth_payload)
                rgb = cls._read_rgb(reader, rgb_payload)
                return rgb, depth, rgb_payload, depth_payload
            except RuntimeError as error:
                last_error = error
                if not cls._is_transient_buffer_error(error):
                    raise
        raise RuntimeError(
            "all synchronized RGB/aligned-depth pairs were recycled before "
            f"copy completed: {last_error}"
        )

    @staticmethod
    def _is_transient_buffer_error(error: Exception) -> bool:
        message = str(error).lower()
        return (
            "bufferref has expired" in message
            or "slot was recycled" in message
            or "consistent shared-memory payload" in message
        )

    @staticmethod
    def _read_rgb(reader: CameraSharedMemory, ref: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(ref)
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            bgr = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise RuntimeError("JPEG RGB frame could not be decoded")
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name == "RGB":
            return np.frombuffer(payload, np.uint8).reshape(height, width, 3).copy()
        if format_name == "BGR":
            bgr = np.frombuffer(payload, np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name in {"RGBA", "BGRA"}:
            raw = "RGBA" if format_name == "RGBA" else "BGRA"
            return np.asarray(Image.frombytes("RGBA", (width, height), payload, "raw", raw).convert("RGB"))
        raise RuntimeError(f"unsupported RGB format: {format_name or 'unknown'}")

    @staticmethod
    def _read_depth(reader: CameraSharedMemory, ref: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(ref)
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format: {format_name or 'unknown'}")
        expected = width * height
        values = np.frombuffer(payload, dtype="<u2", count=expected)
        if values.size != expected:
            raise RuntimeError("aligned depth payload is shorter than declared")
        scale_mm = float(ref.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0

    @staticmethod
    def reference_timestamp(ref: dict[str, Any]) -> int:
        return int(
            ref.get("global_timestamp_us")
            or ref.get("system_timestamp_us")
            or ref.get("device_timestamp_us")
            or 0
        )


def normalized_yx_to_pixel(point_yx: list[int] | tuple[int, int], shape: tuple[int, ...]) -> tuple[int, int]:
    height, width = shape[:2]
    y = int(round(float(point_yx[0]) * max(0, height - 1) / 1000.0))
    x = int(round(float(point_yx[1]) * max(0, width - 1) / 1000.0))
    return max(0, min(height - 1, y)), max(0, min(width - 1, x))


def normalized_box_to_pixels(box: list[int], shape: tuple[int, ...], padding: float = 0.0) -> tuple[int, int, int, int]:
    y0, x0 = normalized_yx_to_pixel(box[:2], shape)
    y1, x1 = normalized_yx_to_pixel(box[2:], shape)
    if y1 < y0:
        y0, y1 = y1, y0
    if x1 < x0:
        x0, x1 = x1, x0
    pad_y = int(round((y1 - y0 + 1) * max(0.0, padding)))
    pad_x = int(round((x1 - x0 + 1) * max(0.0, padding)))
    return (
        max(0, y0 - pad_y),
        max(0, x0 - pad_x),
        min(shape[0] - 1, y1 + pad_y),
        min(shape[1] - 1, x1 + pad_x),
    )


def make_initial_mask(
    rgb: np.ndarray,
    box_1000: list[int],
    positive_points_1000: list[list[int]],
    *,
    padding_fraction: float,
    minimum_pixels: int,
) -> np.ndarray:
    """Create a conservative visible-object mask without changing FoundationPose."""
    y0, x0, y1, x1 = normalized_box_to_pixels(box_1000, rgb.shape, padding_fraction)
    mask = np.zeros(rgb.shape[:2], dtype=np.uint8)
    crop = rgb[y0 : y1 + 1, x0 : x1 + 1]
    points = [normalized_yx_to_pixel(point, rgb.shape) for point in positive_points_1000]
    local_points = [(y - y0, x - x0) for y, x in points if y0 <= y <= y1 and x0 <= x <= x1]
    if crop.size and local_points:
        lab = cv2.cvtColor(crop, cv2.COLOR_RGB2LAB).astype(np.float32)
        seed_colors = np.stack([lab[y, x] for y, x in local_points])
        distance = np.min(np.linalg.norm(lab[:, :, None, :] - seed_colors[None, None, :, :], axis=3), axis=2)
        candidate = (distance <= 42.0).astype(np.uint8)
        candidate = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        component = np.zeros_like(candidate)
        labels_count, labels = cv2.connectedComponents(candidate)
        accepted: set[int] = set()
        for y, x in local_points:
            label = int(labels[y, x])
            if label > 0:
                accepted.add(label)
        for label in accepted:
            component[labels == label] = 255
        mask[y0 : y1 + 1, x0 : x1 + 1] = component
    if int(np.count_nonzero(mask)) < minimum_pixels:
        mask[y0 : y1 + 1, x0 : x1 + 1] = 255
    return mask


def tip_depth_from_near_cluster(
    frame: RgbdFrame,
    point_1000: list[int],
    config: dict[str, Any],
    *,
    permit_local_minimum: bool,
) -> TipDepth:
    center = normalized_yx_to_pixel(point_1000, frame.rgb.shape)
    radius = int(config["search_radius_px"])
    y0, y1 = max(0, center[0] - radius), min(frame.depth_m.shape[0], center[0] + radius + 1)
    x0, x1 = max(0, center[1] - radius), min(frame.depth_m.shape[1], center[1] + radius + 1)
    patch = frame.depth_m[y0:y1, x0:x1]
    valid = patch[
        np.isfinite(patch)
        & (patch >= float(config["minimum_depth_m"]))
        & (patch <= float(config["maximum_depth_m"]))
    ]
    if valid.size == 0:
        raise RuntimeError("no valid aligned depth exists at the VLM beak point")
    if permit_local_minimum:
        near = float(np.percentile(valid, 10))
        span = float(config["maximum_near_cluster_span_m"])
        surface_band = min(span, max(0.008, span * 0.3))
        cluster = valid[(valid >= near - 0.005) & (valid <= near + surface_band)]
        if cluster.size >= int(config["minimum_cluster_pixels"]):
            depth = float(np.median(cluster))
            method = "VLM_POINT_LOCAL_NEAR_CLUSTER"
        else:
            depth = float(frame.depth_m[center])
            method = "VLM_PIXEL_DEPTH_FALLBACK"
    else:
        value = float(frame.depth_m[center])
        if not np.isfinite(value) or value <= 0:
            raise RuntimeError("the exact VLM beak pixel has no valid aligned depth")
        depth = value
        method = "VLM_PIXEL_ONLY"
        cluster = valid
    return TipDepth(
        pixel_yx=center,
        depth_m=depth,
        camera_system_xyz_m=deproject_pixel(
            center,
            depth,
            frame.intrinsics,
        ),
        method=method,
        valid_pixel_count=int(cluster.size),
        cluster_span_m=float(np.ptp(cluster)) if cluster.size else 0.0,
    )


def segmented_surface_depth(
    frame: RgbdFrame,
    mask: np.ndarray,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Estimate the nearest coherent surface inside a segmented object mask."""
    selection = np.asarray(mask) > 0
    if selection.shape != frame.depth_m.shape:
        raise ValueError("segmentation mask and aligned depth shapes differ")
    pixel_y, pixel_x = np.nonzero(selection)
    values = frame.depth_m[pixel_y, pixel_x]
    valid = (
        np.isfinite(values)
        & (values >= float(config["minimum_depth_m"]))
        & (values <= float(config["maximum_depth_m"]))
    )
    values = values[valid]
    pixel_y = pixel_y[valid]
    pixel_x = pixel_x[valid]
    minimum_pixels = int(config["minimum_cluster_pixels"])
    if values.size < minimum_pixels:
        raise RuntimeError(
            "the segmented gripper has too few valid aligned-depth pixels"
        )
    near = float(np.percentile(values, 10.0))
    maximum_span = float(config["maximum_near_cluster_span_m"])
    cluster_selection = (
        (values >= near - 0.005)
        & (values <= near + maximum_span)
    )
    cluster = values[cluster_selection]
    if cluster.size < minimum_pixels:
        raise RuntimeError(
            "the segmented gripper has no coherent foreground depth surface"
        )
    depth_m = float(np.median(cluster))
    mad_m = float(np.median(np.abs(cluster - depth_m)))
    cluster_y = pixel_y[cluster_selection].astype(np.float64)
    cluster_x = pixel_x[cluster_selection].astype(np.float64)
    intrinsics = frame.intrinsics
    camera_x = (
        (cluster_x - float(intrinsics["cx"]))
        * cluster
        / float(intrinsics["fx"])
    )
    camera_y = (
        (cluster_y - float(intrinsics["cy"]))
        * cluster
        / float(intrinsics["fy"])
    )
    camera_point = np.asarray(
        [
            np.median(camera_x),
            np.median(camera_y),
            np.median(cluster),
        ],
        dtype=np.float64,
    )
    return {
        "method": "SEGMENTED_MASK_NEAREST_COHERENT_SURFACE",
        "depth_m": depth_m,
        "camera_system_xyz_m": camera_point.tolist(),
        "median_pixel_yx": [
            float(np.median(cluster_y)),
            float(np.median(cluster_x)),
        ],
        "valid_mask_pixel_count": int(values.size),
        "cluster_pixel_count": int(cluster.size),
        "cluster_span_m": float(np.ptp(cluster)),
        "cluster_mad_m": mad_m,
        "near_percentile_m": near,
        "maximum_cluster_span_m": maximum_span,
    }


def encode_rgb_jpeg(rgb: np.ndarray, quality: int = 92) -> bytes:
    ok, encoded = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("could not encode RGB image")
    return encoded.tobytes()


def encode_depth_png(depth_m: np.ndarray) -> bytes:
    valid = depth_m[np.isfinite(depth_m) & (depth_m > 0)]
    if valid.size:
        near, far = float(np.percentile(valid, 2)), float(np.percentile(valid, 98))
        scale = np.clip((far - depth_m) / max(1e-6, far - near), 0, 1)
        gray = (scale * 255).astype(np.uint8)
        gray[~np.isfinite(depth_m) | (depth_m <= 0)] = 0
    else:
        gray = np.zeros(depth_m.shape, np.uint8)
    ok, encoded = cv2.imencode(".png", gray)
    if not ok:
        raise RuntimeError("could not encode depth image")
    return encoded.tobytes()


def render_overlay(
    rgb: np.ndarray,
    detections: dict[str, Any],
    masks: dict[str, np.ndarray],
    tip_depths: list[TipDepth],
) -> bytes:
    image = Image.fromarray(rgb.copy()).convert("RGBA")
    tint = Image.new("RGBA", image.size, (0, 0, 0, 0))
    tint_pixels = np.asarray(tint).copy()
    colors = {"base": (52, 211, 153, 80), "gripper": (96, 165, 250, 95)}
    for name, mask in masks.items():
        tint_pixels[mask > 0] = colors.get(name, (250, 204, 21, 80))
    image = Image.alpha_composite(image, Image.fromarray(tint_pixels, "RGBA"))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for name, detection in detections.items():
        box = normalized_box_to_pixels(detection["box_2d"], rgb.shape)
        y0, x0, y1, x1 = box
        color = colors.get(name, (250, 204, 21, 255))[:3] + (255,)
        draw.rectangle((x0, y0, x1, y1), outline=color, width=3)
        draw.text((x0 + 4, y0 + 4), name, fill=color, font=font)
        for point in detection["positive_points_2d"]:
            y, x = normalized_yx_to_pixel(point, rgb.shape)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline=color, width=2)
    for index, tip in enumerate(tip_depths):
        y, x = tip.pixel_yx
        gripper = detections.get("gripper") or {}
        if gripper.get("box_2d"):
            gy0, gx0, gy1, gx1 = normalized_box_to_pixels(gripper["box_2d"], rgb.shape)
            start = ((gx0 + gx1) // 2, (gy0 + gy1) // 2)
            draw.line((start[0], start[1], x, y), fill=(255, 159, 67, 255), width=3)
            angle = np.arctan2(y - start[1], x - start[0])
            for offset in (-0.55, 0.55):
                draw.line(
                    (
                        x,
                        y,
                        x - 13 * np.cos(angle + offset),
                        y - 13 * np.sin(angle + offset),
                    ),
                    fill=(255, 159, 67, 255),
                    width=3,
                )
        draw.line((x - 12, y, x + 12, y), fill=(255, 71, 87, 255), width=3)
        draw.line((x, y - 12, x, y + 12), fill=(255, 71, 87, 255), width=3)
        draw.text((x + 8, y + 8), f"beak {index + 1}: {tip.depth_m:.3f}m", fill=(255, 255, 255, 255), font=font)
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=92)
    return output.getvalue()


def save_frame_artifacts(run_dir: Path, frame: RgbdFrame) -> None:
    (run_dir / "camera.jpg").write_bytes(encode_rgb_jpeg(frame.rgb))
    (run_dir / "depth.png").write_bytes(encode_depth_png(frame.depth_m))
