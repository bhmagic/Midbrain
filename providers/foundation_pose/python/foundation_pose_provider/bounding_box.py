"""Bounding-box initialization helpers for FoundationPose sessions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


_COORDINATE_SPACES = {
    "normalized_0_1000",
    "normalized_0_1",
    "pixels",
}


@dataclass(frozen=True)
class BoundingBoxMask:
    """A validated y/x ordered rectangle that can be rasterized as a mask."""

    box_2d: tuple[float, float, float, float]
    coordinate_space: str = "normalized_0_1000"
    padding_fraction: float = 0.0

    def __post_init__(self) -> None:
        if self.coordinate_space not in _COORDINATE_SPACES:
            raise ValueError(
                "bounding box coordinate_space must be one of: "
                + ", ".join(sorted(_COORDINATE_SPACES))
            )
        if len(self.box_2d) != 4:
            raise ValueError("bounding box box_2d must contain [ymin, xmin, ymax, xmax]")
        ymin, xmin, ymax, xmax = self.box_2d
        if not all(math.isfinite(value) for value in self.box_2d):
            raise ValueError("bounding box coordinates must be finite")
        if ymin >= ymax or xmin >= xmax:
            raise ValueError("bounding box must have positive width and height")
        if ymin < 0.0 or xmin < 0.0:
            raise ValueError("bounding box minimum coordinates cannot be negative")
        upper_bound = {
            "normalized_0_1000": 1000.0,
            "normalized_0_1": 1.0,
            "pixels": None,
        }[self.coordinate_space]
        if upper_bound is not None and (ymax > upper_bound or xmax > upper_bound):
            raise ValueError(
                f"bounding box coordinates exceed {self.coordinate_space} bounds"
            )
        if not 0.0 <= self.padding_fraction <= 0.5:
            raise ValueError("bounding box padding_fraction must be between 0 and 0.5")

    @classmethod
    def from_request(cls, request: Mapping[str, Any]) -> "BoundingBoxMask | None":
        raw = request.get("bounding_box")
        if raw is None:
            raw = request.get("box_2d")
        if raw is None:
            return None

        coordinate_space = str(
            request.get("bounding_box_coordinate_space")
            or request.get("bounding_box_format")
            or "normalized_0_1000"
        ).strip().lower()
        padding_fraction = float(request.get("bounding_box_padding_fraction") or 0.0)
        coordinates: Any = raw

        if isinstance(raw, Mapping):
            coordinates = raw.get("box_2d")
            coordinate_space = str(
                raw.get("coordinate_space") or coordinate_space
            ).strip().lower()
            padding_fraction = float(raw.get("padding_fraction", padding_fraction))
            if coordinates is None and all(
                key in raw for key in ("ymin", "xmin", "ymax", "xmax")
            ):
                coordinates = [raw["ymin"], raw["xmin"], raw["ymax"], raw["xmax"]]

        aliases = {
            "gemini": "normalized_0_1000",
            "yxyx_normalized_1000": "normalized_0_1000",
            "normalized": "normalized_0_1",
            "yxyx_normalized": "normalized_0_1",
            "pixel": "pixels",
            "yxyx_pixels": "pixels",
        }
        coordinate_space = aliases.get(coordinate_space, coordinate_space)

        if isinstance(coordinates, (str, bytes)) or not isinstance(coordinates, Sequence):
            raise ValueError("bounding_box must contain box_2d coordinates")
        if len(coordinates) != 4:
            raise ValueError("bounding box box_2d must contain [ymin, xmin, ymax, xmax]")
        return cls(
            tuple(float(value) for value in coordinates),
            coordinate_space=coordinate_space,
            padding_fraction=padding_fraction,
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "box_2d": list(self.box_2d),
            "coordinate_space": self.coordinate_space,
            "padding_fraction": self.padding_fraction,
        }

    def to_mask(self, height: int, width: int) -> np.ndarray:
        if height <= 0 or width <= 0:
            raise ValueError("image dimensions must be positive")
        ymin, xmin, ymax, xmax = self.box_2d
        if self.coordinate_space == "normalized_0_1000":
            scale_y, scale_x = height / 1000.0, width / 1000.0
            ymin, ymax = ymin * scale_y, ymax * scale_y
            xmin, xmax = xmin * scale_x, xmax * scale_x
        elif self.coordinate_space == "normalized_0_1":
            ymin, ymax = ymin * height, ymax * height
            xmin, xmax = xmin * width, xmax * width

        pad_y = (ymax - ymin) * self.padding_fraction
        pad_x = (xmax - xmin) * self.padding_fraction
        y0 = max(0, min(height, math.floor(ymin - pad_y)))
        x0 = max(0, min(width, math.floor(xmin - pad_x)))
        y1 = max(0, min(height, math.ceil(ymax + pad_y)))
        x1 = max(0, min(width, math.ceil(xmax + pad_x)))
        if y0 >= y1 or x0 >= x1:
            raise ValueError("bounding box does not overlap the camera image")

        mask = np.zeros((height, width), dtype=np.bool_)
        mask[y0:y1, x0:x1] = True
        return np.ascontiguousarray(mask)
