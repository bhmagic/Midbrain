from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def _intrinsics(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, dict):
        return (
            float(value["fx"]),
            float(value["fy"]),
            float(value["cx"]),
            float(value["cy"]),
        )
    values = list(value)
    return float(values[0]), float(values[4]), float(values[2]), float(values[5])


@lru_cache(maxsize=8)
def _sample_obj_vertices(
    path_text: str, modified_ns: int, maximum_vertices: int = 18000
) -> np.ndarray:
    del modified_ns
    vertices: list[tuple[float, float, float]] = []
    with Path(path_text).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith("v "):
                continue
            fields = line.split()
            if len(fields) >= 4:
                vertices.append((float(fields[1]), float(fields[2]), float(fields[3])))
    if not vertices:
        raise ValueError("FoundationPose OBJ contains no vertices")
    values = np.asarray(vertices, dtype=np.float64)
    if len(values) > maximum_vertices:
        indices = np.linspace(0, len(values) - 1, maximum_vertices, dtype=np.int64)
        values = values[indices]
    return values


def _project(
    points_camera: np.ndarray,
    intrinsics: Any,
) -> tuple[np.ndarray, np.ndarray]:
    fx, fy, cx, cy = _intrinsics(intrinsics)
    valid = np.isfinite(points_camera).all(axis=1) & (points_camera[:, 2] > 1e-5)
    points = points_camera[valid]
    if not len(points):
        return np.empty((0, 2), dtype=np.float64), valid
    projected = np.column_stack(
        (
            fx * points[:, 0] / points[:, 2] + cx,
            fy * points[:, 1] / points[:, 2] + cy,
        )
    )
    return projected, valid


def render_fit_overlay(
    *,
    rgb_path: Path,
    mask_path: Path,
    mesh_path: Path,
    mesh_scale_to_m: float,
    camera_from_centered_mesh: np.ndarray,
    camera_intrinsics: Any,
    output_path: Path,
    candidate_id: str,
    dilation_radius_px: int,
    label_details: str = "geometry-only review",
    camera_from_axis_frame: np.ndarray | None = None,
) -> Path:
    image = Image.open(rgb_path).convert("RGBA")
    mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8) > 0
    if mask.shape != (image.height, image.width):
        raise ValueError("fit mask shape does not match RGB image")
    tint = np.zeros((image.height, image.width, 4), dtype=np.uint8)
    tint[mask] = (255, 60, 60, 58)
    composite = Image.alpha_composite(image, Image.fromarray(tint))

    vertices = _sample_obj_vertices(
        str(mesh_path.resolve()), mesh_path.stat().st_mtime_ns
    )
    mesh_points = vertices * float(mesh_scale_to_m)
    points_camera = (
        mesh_points @ camera_from_centered_mesh[:3, :3].T
        + camera_from_centered_mesh[:3, 3]
    )
    projected, _ = _project(points_camera, camera_intrinsics)
    if len(projected):
        inside = (
            (projected[:, 0] >= 0)
            & (projected[:, 0] < image.width)
            & (projected[:, 1] >= 0)
            & (projected[:, 1] < image.height)
        )
        projected = projected[inside]
    draw = ImageDraw.Draw(composite)
    if len(projected):
        pixels = [(int(round(x)), int(round(y))) for x, y in projected]
        draw.point(pixels, fill=(0, 230, 255, 205))
        draw.point([(x + 1, y) for x, y in pixels], fill=(0, 180, 255, 145))
        left, top = projected.min(axis=0)
        right, bottom = projected.max(axis=0)
        draw.rectangle(
            (float(left), float(top), float(right), float(bottom)),
            outline=(0, 230, 255, 255),
            width=max(2, image.width // 640),
        )

    axis_pose = (
        camera_from_centered_mesh
        if camera_from_axis_frame is None
        else np.asarray(camera_from_axis_frame, dtype=np.float64)
    )
    if axis_pose.shape != (4, 4):
        raise ValueError("fit-overlay axis pose must be a 4x4 matrix")
    origin = axis_pose[:3, 3]
    axis_length = 0.10
    axis_points = np.vstack(
        [
            origin,
            origin + axis_pose[:3, 0] * axis_length,
            origin + axis_pose[:3, 1] * axis_length,
            origin + axis_pose[:3, 2] * axis_length,
        ]
    )
    axes, valid = _project(axis_points, camera_intrinsics)
    if bool(np.all(valid)) and len(axes) == 4:
        for endpoint, color, label in zip(
            axes[1:],
            ((255, 60, 60, 255), (70, 255, 90, 255), (60, 160, 255, 255)),
            ("X", "Y", "Z"),
        ):
            draw.line(
                [tuple(axes[0]), tuple(endpoint)],
                fill=color,
                width=max(3, image.width // 480),
            )
            draw.text(tuple(endpoint), label, fill=color, font=ImageFont.load_default())

    label = f"{candidate_id}  dilation={dilation_radius_px}px  {label_details}"
    draw.rectangle((0, 0, min(image.width, 980), 36), fill=(0, 0, 0, 225))
    draw.text((10, 10), label, fill="white", font=ImageFont.load_default())
    draw.text(
        (10, image.height - 22),
        "cyan = projected CAD; red = supporting mask",
        fill=(210, 245, 255, 255),
        font=ImageFont.load_default(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.convert("RGB").save(output_path, format="PNG")
    return output_path
