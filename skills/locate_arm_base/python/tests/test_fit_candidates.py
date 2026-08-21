from __future__ import annotations

import numpy as np

from locate_arm_base.fit_candidates import project_axis_vectors


def test_axis_projection_returns_normalized_ui_vectors() -> None:
    camera_from_axis = np.eye(4, dtype=np.float64)
    camera_from_axis[:3, 3] = [0.0, 0.0, 1.0]

    vectors = project_axis_vectors(
        camera_from_axis_frame=camera_from_axis,
        camera_intrinsics={"fx": 100.0, "fy": 100.0, "cx": 100.0, "cy": 50.0},
        image_size=(200, 100),
    )

    by_axis = {value["axis"]: value for value in vectors}
    assert set(by_axis) == {"X", "Y"}
    assert by_axis["X"]["x1"] == 0.5
    assert by_axis["X"]["y1"] == 0.5
    assert by_axis["X"]["x2"] > by_axis["X"]["x1"]
    assert by_axis["Y"]["y2"] > by_axis["Y"]["y1"]
