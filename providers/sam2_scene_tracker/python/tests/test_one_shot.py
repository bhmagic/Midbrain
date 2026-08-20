from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from sam2_scene_tracker.one_shot import segment_workspace_image


class FakeTracker:
    def __init__(self) -> None:
        self.image_shape: tuple[int, int] | None = None
        self.prompt_count = 0
        self.prompts = None

    def set_image(self, image_rgb: np.ndarray) -> None:
        self.image_shape = image_rgb.shape[:2]

    def segment(self, prompts):
        assert self.image_shape is not None
        self.prompt_count = len(prompts)
        self.prompts = prompts
        mask = np.zeros(self.image_shape, dtype=bool)
        mask[2:6, 3:8] = True
        return mask, 0.91


def _rgb(workspace: Path) -> tuple[Path, str]:
    path = workspace / "frame.png"
    Image.fromarray(np.full((8, 10, 3), 127, dtype=np.uint8)).save(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_one_shot_segmentation_preserves_prompt_and_artifact_provenance(
    tmp_path: Path,
) -> None:
    image_path, image_sha256 = _rgb(tmp_path)
    tracker = FakeTracker()

    result = segment_workspace_image(
        payload={
            "request_id": "candidate-1",
            "rgb_path": str(image_path),
            "rgb_sha256": image_sha256,
            "box_yxyx": [100, 100, 900, 900],
            "positive_points_yx": [[400, 400]],
            "negative_points_yx": [[850, 500]],
            "prompt_confidence": 0.8,
        },
        tracker=tracker,
        tracker_lock=threading.Lock(),
        workspace_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        provider_id="perception.sam2_scene_tracker",
        provider_instance_id="instance-1",
        boot_id="boot-1",
    )

    assert tracker.prompt_count == 1
    assert tracker.prompts[0].positive_points_yx == ((400, 400),)
    assert tracker.prompts[0].negative_points_yx == ((850, 500),)
    assert result["status"] == "SEGMENTED"
    assert result["quality"]["sam2_score"] == 0.91
    assert result["mask_artifact"]["nonzero_pixels"] == 20
    assert Path(result["mask_artifact"]["path"]).is_file()
    assert result["provenance"]["rgb_sha256"] == image_sha256


def test_one_shot_segmentation_rejects_rgb_digest_mismatch(
    tmp_path: Path,
) -> None:
    image_path, _ = _rgb(tmp_path)
    try:
        segment_workspace_image(
            payload={
                "rgb_path": str(image_path),
                "rgb_sha256": "0" * 64,
                "box_yxyx": [100, 100, 900, 900],
                "positive_points_yx": [[400, 400], [600, 600]],
                "prompt_confidence": 0.8,
            },
            tracker=FakeTracker(),
            tracker_lock=threading.Lock(),
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            provider_id="provider",
            provider_instance_id="instance",
            boot_id="boot",
        )
    except ValueError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("a mismatched RGB digest must fail closed")
