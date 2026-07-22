from __future__ import annotations

import numpy as np
import pytest

from foundation_pose_provider.bounding_box import BoundingBoxMask


def test_gemini_box_rasterizes_at_camera_resolution() -> None:
    box = BoundingBoxMask.from_request({"box_2d": [250, 100, 750, 600]})
    assert box is not None
    mask = box.to_mask(100, 200)
    assert mask.dtype == np.bool_
    assert mask.shape == (100, 200)
    assert int(mask.sum()) == 50 * 100
    assert mask[25, 20]
    assert not mask[24, 20]


def test_pixel_box_padding_and_clipping() -> None:
    box = BoundingBoxMask(
        (10, 20, 30, 40),
        coordinate_space="pixels",
        padding_fraction=0.5,
    )
    mask = box.to_mask(35, 45)
    assert int(mask.sum()) == 35 * 35
    assert mask[0, 10]


@pytest.mark.parametrize(
    "box_request",
    [
        {"box_2d": [1, 2, 1, 3]},
        {"box_2d": [-1, 2, 3, 4]},
        {"box_2d": [0, 0, 1001, 2]},
        {"bounding_box": {"box_2d": [0, 0, 1, 1], "coordinate_space": "unknown"}},
    ],
)
def test_invalid_boxes_are_rejected(box_request: dict) -> None:
    with pytest.raises(ValueError):
        BoundingBoxMask.from_request(box_request)


def test_mapping_shape_and_alias_are_supported() -> None:
    box = BoundingBoxMask.from_request(
        {
            "bounding_box": {
                "ymin": 0.1,
                "xmin": 0.2,
                "ymax": 0.5,
                "xmax": 0.6,
                "coordinate_space": "normalized",
            }
        }
    )
    assert box is not None
    assert box.coordinate_space == "normalized_0_1"
    assert int(box.to_mask(100, 100).sum()) == 40 * 40
