from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .math3d import normalized_yx_to_pixel


def render_plan_overlay(
    rgb: np.ndarray,
    scene: dict[str, Any],
    cut_pixels: list[dict[str, list[int]]],
    *,
    blade_registration_candidate: dict[str, Any] | None = None,
) -> bytes:
    image = Image.fromarray(rgb.copy()).convert("RGBA")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    colors = {
        "board": (48, 211, 142, 255),
        "vegetable": (255, 187, 51, 255),
        "blade": (255, 99, 132, 255),
    }
    for name in ("board", "vegetable"):
        polygon = scene[name].get("polygon_yx_1000")
        if not polygon:
            continue
        pixels = [
            tuple(reversed(normalized_yx_to_pixel(point, rgb.shape)))
            for point in polygon
        ]
        draw.line([*pixels, pixels[0]], fill=colors[name], width=4)
        draw.text(pixels[0], name, fill=colors[name], font=font)

    blade = scene.get("blade") or {}
    tip_yx_1000 = blade.get("tip_yx_1000")
    heel_yx_1000 = blade.get("heel_yx_1000")
    if (
        bool(blade.get("visible"))
        and tip_yx_1000 is not None
        and heel_yx_1000 is not None
    ):
        tip_yx = normalized_yx_to_pixel(tip_yx_1000, rgb.shape)
        heel_yx = normalized_yx_to_pixel(heel_yx_1000, rgb.shape)
        tip = (tip_yx[1], tip_yx[0])
        heel = (heel_yx[1], heel_yx[0])
        draw.line([heel, tip], fill=colors["blade"], width=5)
        draw.text(tip, "blade (informational)", fill=colors["blade"], font=font)
    junction_yx_1000 = blade.get("blade_handle_junction_yx_1000")
    handle_anchor_yx_1000 = blade.get("handle_depth_anchor_yx_1000")
    if junction_yx_1000 and handle_anchor_yx_1000:
        junction_yx = normalized_yx_to_pixel(
            junction_yx_1000,
            rgb.shape,
        )
        handle_anchor_yx = normalized_yx_to_pixel(
            handle_anchor_yx_1000,
            rgb.shape,
        )
        junction = (junction_yx[1], junction_yx[0])
        handle_anchor = (handle_anchor_yx[1], handle_anchor_yx[0])
        draw.line(
            [junction, handle_anchor],
            fill=(255, 235, 59, 255),
            width=3,
        )
        draw.ellipse(
            (
                junction[0] - 5,
                junction[1] - 5,
                junction[0] + 5,
                junction[1] + 5,
            ),
            outline=(255, 153, 51, 255),
            width=3,
        )
        draw.ellipse(
            (
                handle_anchor[0] - 7,
                handle_anchor[1] - 7,
                handle_anchor[0] + 7,
                handle_anchor[1] + 7,
            ),
            outline=(255, 235, 59, 255),
            width=4,
        )
        draw.text(
            (junction[0] + 8, junction[1] - 18),
            "blade/handle junction",
            fill=(255, 153, 51, 255),
            font=font,
        )
        draw.text(
            (handle_anchor[0] + 10, handle_anchor[1] + 4),
            "non-reflective depth anchor",
            fill=(255, 235, 59, 255),
            font=font,
        )
    if blade_registration_candidate:
        candidate_yx = blade_registration_candidate.get(
            "acting_point_image_yx_1000"
        )
        if candidate_yx:
            y, x = normalized_yx_to_pixel(candidate_yx, rgb.shape)
            accepted = (
                blade_registration_candidate.get("status")
                == "CANDIDATE_REVIEW_REQUIRED"
            )
            color = (46, 230, 255, 255) if accepted else (255, 72, 72, 255)
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline=color, width=4)
            label = (
                "5 cm acting point candidate"
                if accepted
                else "blade registration rejected"
            )
            draw.text((x + 10, y - 7), label, fill=color, font=font)

    for cut in cut_pixels:
        entry_yx = cut["entry_yx"]
        exit_yx = cut["exit_yx"]
        entry = (entry_yx[1], entry_yx[0])
        exit_point = (exit_yx[1], exit_yx[0])
        draw.line([entry, exit_point], fill=(95, 182, 255, 255), width=3)
        draw.text(entry, str(cut["index"] + 1), fill=(255, 255, 255, 255), font=font)

    draw.rectangle((8, 8, 410, 34), fill=(12, 18, 30, 210))
    draw.text(
        (15, 15),
        "PLAN REVIEW - NO MOTION YET - OPERATOR TAKEOVER REQUIRED",
        fill=(255, 255, 255, 255),
        font=font,
    )
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=92)
    return output.getvalue()


def render_first_cut_target_overlay(
    rgb: np.ndarray,
    *,
    entry_camera_m: list[float],
    exit_camera_m: list[float],
    intrinsics: dict[str, Any],
    board_entry_camera_m: list[float] | None = None,
    board_exit_camera_m: list[float] | None = None,
) -> bytes:
    image = Image.fromarray(rgb.copy()).convert("RGBA")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    def project(point_m: list[float]) -> tuple[int, int]:
        point = np.asarray(point_m, dtype=np.float64)
        if point.shape != (3,) or not np.all(np.isfinite(point)):
            raise ValueError("first-cut overlay point must be a finite 3-vector")
        if point[2] <= 0.0:
            raise ValueError("first-cut overlay point is behind the camera")
        x = int(
            round(
                float(intrinsics["fx"]) * point[0] / point[2]
                + float(intrinsics["cx"])
            )
        )
        y = int(
            round(
                float(intrinsics["fy"]) * point[1] / point[2]
                + float(intrinsics["cy"])
            )
        )
        return x, y

    if (board_entry_camera_m is None) != (board_exit_camera_m is None):
        raise ValueError(
            "both board-plane first-cut overlay points must be provided"
        )
    if board_entry_camera_m is not None and board_exit_camera_m is not None:
        board_entry = project(board_entry_camera_m)
        board_exit = project(board_exit_camera_m)
        board_center = (
            int(round((board_entry[0] + board_exit[0]) / 2.0)),
            int(round((board_entry[1] + board_exit[1]) / 2.0)),
        )
        board_color = (255, 166, 36, 255)
        draw.line([board_entry, board_exit], fill=board_color, width=5)
        draw.ellipse(
            (
                board_center[0] - 8,
                board_center[1] - 8,
                board_center[0] + 8,
                board_center[1] + 8,
            ),
            outline=(255, 255, 255, 255),
            fill=(255, 166, 36, 180),
            width=3,
        )
        draw.text(
            (board_center[0] + 11, board_center[1] + 8),
            "orange: board cut",
            fill=board_color,
            font=font,
        )

    entry = project(entry_camera_m)
    exit_point = project(exit_camera_m)
    center = (
        int(round((entry[0] + exit_point[0]) / 2.0)),
        int(round((entry[1] + exit_point[1]) / 2.0)),
    )
    color = (36, 180, 255, 255)
    draw.line([entry, exit_point], fill=color, width=5)
    draw.ellipse(
        (
            center[0] - 9,
            center[1] - 9,
            center[0] + 9,
            center[1] + 9,
        ),
        outline=(255, 255, 255, 255),
        fill=(36, 180, 255, 180),
        width=3,
    )
    draw.text(
        (center[0] + 12, center[1] - 8),
        "blue: review-height blade",
        fill=color,
        font=font,
    )
    draw.rectangle((8, 8, 500, 34), fill=(12, 18, 30, 210))
    draw.text(
        (15, 15),
        "FIRST-CUT CHECK - REVIEW HEIGHT + BOARD TARGET",
        fill=(255, 255, 255, 255),
        font=font,
    )
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=94)
    return output.getvalue()
