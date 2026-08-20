from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from foundation_pose_provider.evidence import load_evidence


def test_replay_evidence_is_contiguous_and_metric(tmp_path: Path) -> None:
    rgb_path, depth_path, mask_path = tmp_path / "rgb.png", tmp_path / "depth.npy", tmp_path / "mask.png"
    Image.fromarray(np.zeros((8, 10, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((8, 10), dtype=np.float32), allow_pickle=False)
    Image.fromarray(np.full((8, 10), 255, dtype=np.uint8)).save(mask_path)
    frame = load_evidence(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask": {"path": str(mask_path)},
        }
    )
    assert frame.rgb.shape == (8, 10, 3)
    assert frame.depth_m.dtype == np.float32
    assert frame.mask.dtype == np.uint8
    assert frame.rgb.flags.c_contiguous and frame.depth_m.flags.c_contiguous


def test_mask_shape_mismatch_is_rejected(tmp_path: Path) -> None:
    rgb_path, depth_path, mask_path = tmp_path / "rgb.png", tmp_path / "depth.npy", tmp_path / "mask.png"
    Image.fromarray(np.zeros((8, 10, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((8, 10), dtype=np.float32), allow_pickle=False)
    Image.fromarray(np.ones((4, 5), dtype=np.uint8)).save(mask_path)
    try:
        load_evidence({"rgb_path": str(rgb_path), "depth_npy_path": str(depth_path), "mask": {"path": str(mask_path)}})
    except ValueError as exc:
        assert "mask shape" in str(exc)
    else:
        raise AssertionError("mismatched mask was accepted")

