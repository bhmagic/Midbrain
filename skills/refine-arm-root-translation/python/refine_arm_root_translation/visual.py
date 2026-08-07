from __future__ import annotations

from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .geometry import (
    apply_transform,
    finite_vector,
    project_camera_point,
    rigid_transform,
)
from .landmark import canonical_yx_to_pixel


def _normalized_pixel(pixel_yx: list[int], grid: tuple[int, int]) -> tuple[float, float]:
    height, width = int(grid[0]), int(grid[1])
    y, x = int(pixel_yx[0]), int(pixel_yx[1])
    if height <= 0 or width <= 0 or not (0 <= y < height and 0 <= x < width):
        raise ValueError("annotation pixel is outside its image grid")
    return (x + 0.5) / width, (y + 0.5) / height


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=6)
    return output.getvalue()


def build_rgbd_visual_channels(
    rgb: np.ndarray,
    registered_depth_m: np.ndarray,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    color = np.asarray(rgb)
    depth = np.asarray(registered_depth_m, dtype=np.float64)
    if color.ndim != 3 or color.shape[2] != 3:
        raise ValueError("rgb must have shape HxWx3")
    if depth.ndim != 2:
        raise ValueError("registered_depth_m must be two-dimensional")
    if color.dtype != np.uint8:
        color = np.asarray(np.clip(color, 0, 255), dtype=np.uint8)
    depth_height, depth_width = depth.shape
    rgb_image = Image.fromarray(color, mode="RGB")
    rgb_on_depth_image = rgb_image.resize(
        (depth_width, depth_height),
        resample=Image.Resampling.BILINEAR,
    )
    rgb_on_depth = np.asarray(rgb_on_depth_image, dtype=np.uint8)
    valid = np.isfinite(depth) & (depth >= 0.05) & (depth <= 20.0)
    depth_rgb = np.zeros((depth_height, depth_width, 3), dtype=np.uint8)
    if np.any(valid):
        values = depth[valid]
        near = float(np.percentile(values, 2.0))
        far = float(np.percentile(values, 98.0))
        if far <= near:
            far = near + 1e-6
        normalized = np.zeros_like(depth, dtype=np.float64)
        normalized[valid] = np.clip(
            (depth[valid] - near) / (far - near),
            0.0,
            1.0,
        )
        intensity = np.zeros_like(depth, dtype=np.uint8)
        intensity[valid] = np.asarray(
            np.round((1.0 - normalized[valid]) * 255.0),
            dtype=np.uint8,
        )
        depth_rgb[..., 0] = intensity
        depth_rgb[..., 1] = np.asarray(
            np.round(255.0 * (1.0 - np.abs(2.0 * normalized - 1.0))),
            dtype=np.uint8,
        )
        depth_rgb[..., 2] = np.asarray(np.round(normalized * 255.0), dtype=np.uint8)
    depth_rgb[~valid] = np.asarray([255, 0, 255], dtype=np.uint8)
    overlap = np.asarray(
        np.round(0.62 * rgb_on_depth + 0.38 * depth_rgb),
        dtype=np.uint8,
    )
    overlap[~valid] = np.asarray(
        np.round(
            0.25 * rgb_on_depth[~valid]
            + 0.75 * np.asarray([255, 0, 255], dtype=np.float64)
        ),
        dtype=np.uint8,
    )
    depth_validity = np.empty((depth_height, depth_width, 3), dtype=np.uint8)
    depth_validity[valid] = np.asarray([245, 245, 245], dtype=np.uint8)
    depth_validity[~valid] = np.asarray([255, 0, 255], dtype=np.uint8)

    def channel(
        channel_id: str,
        label: str,
        image_array: np.ndarray,
    ) -> dict[str, Any]:
        height, width = image_array.shape[:2]
        return {
            "id": channel_id,
            "label": label,
            "image_bytes": _png_bytes(Image.fromarray(image_array, mode="RGB")),
            "media_type": "image/png",
            "width": int(width),
            "height": int(height),
        }

    return (
        [
            channel("rgb", "Exact RGB VLM input", color),
            channel(
                "depth",
                "Registered Depth (MAGENTA = invalid or missing)",
                depth_rgb,
            ),
            channel(
                "depth_validity",
                "Registered Depth Validity (WHITE = usable, MAGENTA = invalid)",
                depth_validity,
            ),
            channel("rgb_depth", "RGB + Registered Depth", overlap),
        ],
        overlap,
    )


def build_visual_annotations(
    *,
    detection: dict[str, Any],
    resolved_landmark: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
    rgb_channel_id: str = "rgb",
    depth_channel_id: str = "depth",
    overlap_channel_id: str = "rgb_depth",
    review_channel_id: str = "marked_overlap",
    alignment_projections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resolved_by_id = {
        point["point_id"]: point
        for point in resolved_landmark.get("registered_points") or []
    }
    annotations = build_detection_annotations(
        detection=detection,
        rgb_grid=rgb_grid,
        registered_depth_grid=registered_depth_grid,
        rgb_channel_id=rgb_channel_id,
        depth_channel_id=depth_channel_id,
        overlap_channel_id=overlap_channel_id,
        review_channel_id=review_channel_id,
    )
    for point in detection.get("points") or []:
        point_id = str(point["point_id"])
        if point_id not in resolved_by_id:
            raise ValueError(f"resolved landmark is missing {point_id}")
    midpoint = resolved_landmark.get("registered_depth_landmark_pixel_yx")
    if isinstance(midpoint, list) and len(midpoint) == 2:
        midpoint_x, midpoint_y = _normalized_pixel(
            midpoint,
            registered_depth_grid,
        )
        landmark_id = str(
            resolved_landmark.get("landmark_id") or "selected visual landmark"
        )
        annotations.append(
            {
                "id": "derived-landmark-midpoint",
                "type": "point",
                "label": f"Observed 3D midpoint: {landmark_id}",
                "confidence": (
                    "high"
                    if resolved_landmark.get("eligible_for_translation_refinement")
                    else "low"
                ),
                "applies_to_channels": [overlap_channel_id, review_channel_id],
                "x": midpoint_x,
                "y": midpoint_y,
            }
        )
    for projection in alignment_projections or []:
        display_xy = projection.get("display_normalized_xy")
        if not isinstance(display_xy, list) or len(display_xy) != 2:
            continue
        label = str(projection.get("label") or "Arm-base origin")
        if projection.get("in_image") is False:
            label += " (off-image direction marker)"
        annotations.append(
            {
                "id": str(projection["annotation_id"]),
                "type": "point",
                "label": label,
                "confidence": "high",
                "applies_to_channels": [
                    rgb_channel_id,
                    depth_channel_id,
                    overlap_channel_id,
                    review_channel_id,
                ],
                "x": float(display_xy[0]),
                "y": float(display_xy[1]),
            }
        )
    return annotations


def build_alignment_image_projections(
    *,
    source_world_from_base: Any,
    proposed_world_from_base: Any,
    base_from_tool: Any,
    tool_landmark_point_m: Any,
    world_from_camera: Any,
    intrinsics: Any,
    registered_depth_grid: tuple[int, int],
) -> list[dict[str, Any]]:
    height, width = int(registered_depth_grid[0]), int(registered_depth_grid[1])
    if height <= 0 or width <= 0:
        raise ValueError("registered_depth_grid must be positive")
    camera_from_world = rigid_transform(
        np.linalg.inv(rigid_transform(world_from_camera, "world_from_camera")),
        "camera_from_world",
    )
    source = rigid_transform(source_world_from_base, "source_world_from_base")
    proposed = rigid_transform(
        proposed_world_from_base,
        "proposed_world_from_base",
    )
    fk = rigid_transform(base_from_tool, "base_from_tool")
    tool_point = finite_vector(tool_landmark_point_m, "tool_landmark_point_m")
    base_landmark_point = apply_transform(fk, tool_point)
    projections: list[dict[str, Any]] = []
    definitions = (
        (
            "old-arm-base-origin",
            "Old active arm-base origin",
            source[:3, 3],
        ),
        (
            "new-arm-base-origin",
            "Proposed arm-base origin",
            proposed[:3, 3],
        ),
        (
            "old-alignment-landmark",
            "Old FK prediction for selected visual landmark",
            apply_transform(source, base_landmark_point),
        ),
        (
            "new-alignment-landmark",
            "Proposed FK prediction for selected visual landmark",
            apply_transform(proposed, base_landmark_point),
        ),
    )
    for annotation_id, label, world_point in definitions:
        camera_point = apply_transform(camera_from_world, world_point)
        projection: dict[str, Any] = {
            "annotation_id": annotation_id,
            "label": label,
            "world_point_m": world_point.tolist(),
            "camera_point_m": camera_point.tolist(),
            "in_front_of_camera": bool(camera_point[2] > 0.0),
            "in_image": False,
            "pixel_yx": None,
            "display_pixel_yx": None,
            "display_normalized_xy": None,
        }
        if camera_point[2] > 0.0:
            pixel_y, pixel_x = project_camera_point(camera_point, intrinsics)
            in_image = 0.0 <= pixel_y < height and 0.0 <= pixel_x < width
            display_y = float(np.clip(pixel_y, 0.0, height - 1.0))
            display_x = float(np.clip(pixel_x, 0.0, width - 1.0))
            projection.update(
                {
                    "in_image": in_image,
                    "pixel_yx": [pixel_y, pixel_x],
                    "display_pixel_yx": [display_y, display_x],
                    "display_normalized_xy": [
                        (display_x + 0.5) / width,
                        (display_y + 0.5) / height,
                    ],
                }
            )
        projections.append(projection)
    return projections


def build_detection_annotations(
    *,
    detection: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_grid: tuple[int, int],
    rgb_channel_id: str = "rgb",
    depth_channel_id: str = "depth",
    depth_validity_channel_id: str | None = "depth_validity",
    overlap_channel_id: str = "rgb_depth",
    review_channel_id: str = "marked_overlap",
) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for point in detection.get("points") or []:
        point_id = str(point["point_id"])
        rgb_pixel_yx = canonical_yx_to_pixel(
            point["rgb_yx_0_1000"],
            grid=rgb_grid,
            name=f"{point_id}.rgb_yx_0_1000",
        )
        depth_pixel_yx = canonical_yx_to_pixel(
            point["registered_depth_yx_0_1000"],
            grid=registered_depth_grid,
            name=f"{point_id}.registered_depth_yx_0_1000",
        )
        rgb_x, rgb_y = _normalized_pixel(rgb_pixel_yx, rgb_grid)
        depth_x, depth_y = _normalized_pixel(
            depth_pixel_yx,
            registered_depth_grid,
        )
        confidence = (
            "high"
            if min(
                float(point["confidence"]),
                float(point["same_surface_confidence"]),
            ) >= 0.85
            else "medium"
        )
        depth_channels = [
            depth_channel_id,
            overlap_channel_id,
            review_channel_id,
        ]
        if depth_validity_channel_id is not None:
            depth_channels.insert(1, depth_validity_channel_id)
        annotations.extend(
            [
                {
                    "id": f"{point_id}-rgb",
                    "type": "point",
                    "label": f"{point_id} RGB feature",
                    "confidence": confidence,
                    "applies_to_channels": [rgb_channel_id],
                    "x": rgb_x,
                    "y": rgb_y,
                },
                {
                    "id": f"{point_id}-depth",
                    "type": "point",
                    "label": f"{point_id} registered depth",
                    "confidence": confidence,
                    "applies_to_channels": depth_channels,
                    "x": depth_x,
                    "y": depth_y,
                },
            ]
        )
    return annotations


def render_marked_overlap_png(
    rgb_on_registered_depth_grid: np.ndarray,
    *,
    detection: dict[str, Any],
    resolved_landmark: dict[str, Any],
    alignment_projections: list[dict[str, Any]] | None = None,
) -> bytes:
    image_array = np.asarray(rgb_on_registered_depth_grid)
    if image_array.ndim != 3 or image_array.shape[2] != 3:
        raise ValueError("rgb_on_registered_depth_grid must have shape HxWx3")
    if image_array.dtype != np.uint8:
        image_array = np.asarray(np.clip(image_array, 0, 255), dtype=np.uint8)
    image = Image.fromarray(image_array, mode="RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    palette = ["#ff4d4d", "#33dd88", "#55aaff", "#ffcc44"]
    radius = max(5, int(round(min(image.size) * 0.012)))
    line_width = max(2, radius // 3)
    for index, point in enumerate(detection.get("points") or []):
        y, x = canonical_yx_to_pixel(
            point["registered_depth_yx_0_1000"],
            grid=image_array.shape[:2],
            name=f"{point['point_id']}.registered_depth_yx_0_1000",
        )
        color = palette[index % len(palette)]
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline=color,
            width=line_width,
        )
        draw.line((x - radius, y, x + radius, y), fill=color, width=line_width)
        draw.line((x, y - radius, x, y + radius), fill=color, width=line_width)
        draw.text(
            (x + radius + 3, max(0, y - radius)),
            str(point["point_id"]),
            fill=color,
            stroke_width=2,
            stroke_fill="#000000",
            font=font,
        )
    midpoint = resolved_landmark.get("registered_depth_landmark_pixel_yx")
    if isinstance(midpoint, list) and len(midpoint) == 2:
        y, x = int(midpoint[0]), int(midpoint[1])
        color = "#ffffff"
        draw.rectangle(
            (x - radius, y - radius, x + radius, y + radius),
            outline=color,
            width=line_width,
        )
        draw.text(
            (x + radius + 3, max(0, y - radius)),
            "derived 3D midpoint",
            fill=color,
            stroke_width=2,
            stroke_fill="#000000",
            font=font,
        )
    projection_points: list[tuple[dict[str, Any], int, int]] = []
    for projection in alignment_projections or []:
        display_yx = projection.get("display_pixel_yx")
        if not isinstance(display_yx, list) or len(display_yx) != 2:
            continue
        projection_points.append(
            (
                projection,
                int(round(float(display_yx[0]))),
                int(round(float(display_yx[1]))),
            )
        )
    points_by_id = {
        str(projection["annotation_id"]): (projection, y, x)
        for projection, y, x in projection_points
    }
    for old_id, new_id, color in (
        ("old-arm-base-origin", "new-arm-base-origin", "#ff66dd"),
        ("old-alignment-landmark", "new-alignment-landmark", "#ff9933"),
    ):
        if old_id in points_by_id and new_id in points_by_id:
            _, old_y, old_x = points_by_id[old_id]
            _, new_y, new_x = points_by_id[new_id]
            draw.line(
                (old_x, old_y, new_x, new_y),
                fill=color,
                width=line_width,
            )
    styles = {
        "old-arm-base-origin": ("#ff66dd", "circle", "OLD base origin"),
        "new-arm-base-origin": ("#00ffff", "diamond", "PROPOSED base origin"),
        "old-alignment-landmark": (
            "#ff9933",
            "square",
            "OLD FK selected landmark",
        ),
        "new-alignment-landmark": (
            "#66ff66",
            "triangle",
            "PROPOSED FK selected landmark",
        ),
    }
    for projection, y, x in projection_points:
        annotation_id = str(projection["annotation_id"])
        color, shape, label = styles[annotation_id]
        if shape == "circle":
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                outline=color,
                width=line_width,
            )
        elif shape == "diamond":
            vertices = [
                (x, y - radius),
                (x + radius, y),
                (x, y + radius),
                (x - radius, y),
                (x, y - radius),
            ]
            draw.line(vertices, fill=color, width=line_width, joint="curve")
        elif shape == "square":
            draw.rectangle(
                (x - radius, y - radius, x + radius, y + radius),
                outline=color,
                width=line_width,
            )
        else:
            vertices = [
                (x, y - radius),
                (x + radius, y + radius),
                (x - radius, y + radius),
                (x, y - radius),
            ]
            draw.line(vertices, fill=color, width=line_width, joint="curve")
        if projection.get("in_image") is False:
            label += " (off-image)"
        draw.text(
            (x + radius + 3, max(0, y - radius)),
            label,
            fill=color,
            stroke_width=2,
            stroke_fill="#000000",
            font=font,
        )
    return _png_bytes(image)


def render_landmark_review_crop_png(
    marked_overlap_png: bytes,
    *,
    detection: dict[str, Any],
    registered_depth_grid: tuple[int, int],
    minimum_margin_px: int = 128,
    minimum_output_size_px: int = 640,
) -> tuple[bytes, list[dict[str, Any]], list[int]]:
    height, width = int(registered_depth_grid[0]), int(registered_depth_grid[1])
    if height <= 0 or width <= 0:
        raise ValueError("registered_depth_grid is invalid")
    points = list(detection.get("points") or [])
    if not points:
        raise ValueError("landmark review crop requires detected points")
    margin = int(minimum_margin_px)
    panel_size = int(minimum_output_size_px)
    if margin < 1 or panel_size < 1:
        raise ValueError("landmark review crop sizes must be positive")
    with Image.open(BytesIO(marked_overlap_png)) as source:
        image = source.convert("RGB")
        if image.size != (width, height):
            raise ValueError("marked overlap dimensions do not match depth grid")
        montage = Image.new(
            "RGB",
            (panel_size * len(points), panel_size),
            color=(0, 0, 0),
        )
        panels: list[dict[str, Any]] = []
        for index, point in enumerate(points):
            y, x = canonical_yx_to_pixel(
                point["registered_depth_yx_0_1000"],
                grid=registered_depth_grid,
                name=f"{point['point_id']}.registered_depth_yx_0_1000",
            )
            top = max(0, y - margin)
            bottom = min(height, y + margin + 1)
            left = max(0, x - margin)
            right = min(width, x + margin + 1)
            crop = image.crop((left, top, right, bottom))
            scale = min(
                float(panel_size) / float(crop.width),
                float(panel_size) / float(crop.height),
            )
            resized = crop.resize(
                (
                    max(1, int(round(crop.width * scale))),
                    max(1, int(round(crop.height * scale))),
                ),
                resample=Image.Resampling.BICUBIC,
            )
            offset_x = index * panel_size + (panel_size - resized.width) // 2
            offset_y = (panel_size - resized.height) // 2
            montage.paste(resized, (offset_x, offset_y))
            draw = ImageDraw.Draw(montage)
            draw.text(
                (index * panel_size + 12, 12),
                str(point["point_id"]),
                fill="#FFFFFF",
                stroke_width=2,
                stroke_fill="#000000",
                font=ImageFont.load_default(),
            )
            panels.append(
                {
                    "point_id": str(point["point_id"]),
                    "source_bounds_yxyx": [top, left, bottom, right],
                    "montage_bounds_xyxy": [
                        index * panel_size,
                        0,
                        (index + 1) * panel_size,
                        panel_size,
                    ],
                    "montage_point_xy": [
                        offset_x + (x - left + 0.5) * scale,
                        offset_y + (y - top + 0.5) * scale,
                    ],
                }
            )
    return (
        _png_bytes(montage),
        panels,
        [int(montage.height), int(montage.width)],
    )


def build_landmark_review_crop_annotations(
    *,
    crop_panels: list[dict[str, Any]],
    crop_grid: list[int],
    channel_id: str = "landmark_review_crop",
) -> list[dict[str, Any]]:
    if len(crop_grid) != 2:
        raise ValueError("crop_grid must contain height and width")
    crop_height, crop_width = [int(value) for value in crop_grid]
    if crop_height < 1 or crop_width < 1 or not crop_panels:
        raise ValueError("landmark review crop metadata is invalid")
    annotations: list[dict[str, Any]] = []
    for panel in crop_panels:
        point_x, point_y = [float(value) for value in panel["montage_point_xy"]]
        annotations.append(
            {
                "id": f"{panel['point_id']}-review-crop",
                "type": "point",
                "label": f"{panel['point_id']} review crop",
                "confidence": "high",
                "applies_to_channels": [channel_id],
                "x": point_x / crop_width,
                "y": point_y / crop_height,
            }
        )
    return annotations
