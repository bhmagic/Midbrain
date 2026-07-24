from __future__ import annotations

import asyncio

import numpy as np
import pytest

import stationary_world_arm_alignment.camera as camera_module
from stationary_world_arm_alignment.camera import (
    RgbdCapture,
    RgbdFrame,
    make_initial_mask,
    tip_depth_from_near_cluster,
)


def frame(depth: np.ndarray) -> RgbdFrame:
    return RgbdFrame(
        rgb=np.zeros((*depth.shape, 3), np.uint8),
        depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 10.0, "cy": 10.0},
        timestamp_us=1,
        frame_number=1,
        camera_frame="camera",
        session_epoch="epoch",
        world_frame="vio",
        calibration_revision="test",
        observations={},
    )


def test_mask_falls_back_to_box_when_color_segmentation_is_too_small() -> None:
    rgb = np.zeros((20, 20, 3), np.uint8)
    mask = make_initial_mask(
        rgb,
        [250, 250, 750, 750],
        [[500, 500], [550, 550]],
        padding_fraction=0,
        minimum_pixels=200,
    )
    assert np.count_nonzero(mask) >= 100


def test_local_cluster_selects_near_beak() -> None:
    depth = np.full((21, 21), 1.0, np.float32)
    depth[8:13, 8:13] = 0.96
    result = tip_depth_from_near_cluster(
        frame(depth),
        [500, 500],
        {
            "search_radius_px": 5,
            "maximum_near_cluster_span_m": 0.05,
            "minimum_cluster_pixels": 6,
            "minimum_depth_m": 0.15,
            "maximum_depth_m": 5.0,
        },
        permit_local_minimum=True,
    )
    assert abs(result.depth_m - 0.96) < 0.01
    assert result.method == "VLM_POINT_LOCAL_NEAR_CLUSTER"


def test_occluded_tip_uses_exact_vlm_pixel() -> None:
    depth = np.full((21, 21), 1.0, np.float32)
    depth[10, 10] = 0.98
    depth[8, 8] = 0.5
    result = tip_depth_from_near_cluster(
        frame(depth),
        [500, 500],
        {
            "search_radius_px": 5,
            "maximum_near_cluster_span_m": 0.05,
            "minimum_cluster_pixels": 6,
            "minimum_depth_m": 0.15,
            "maximum_depth_m": 5.0,
        },
        permit_local_minimum=False,
    )
    assert abs(result.depth_m - 0.98) < 0.01
    assert result.method == "VLM_PIXEL_ONLY"


def test_occluded_tip_fails_when_exact_vlm_pixel_has_no_depth() -> None:
    depth = np.full((21, 21), 1.0, np.float32)
    depth[10, 10] = 0.0
    with pytest.raises(RuntimeError, match="exact VLM beak pixel"):
        tip_depth_from_near_cluster(
            frame(depth),
            [500, 500],
            {
                "search_radius_px": 5,
                "maximum_near_cluster_span_m": 0.05,
                "minimum_cluster_pixels": 6,
                "minimum_depth_m": 0.15,
                "maximum_depth_m": 5.0,
            },
            permit_local_minimum=False,
        )


def test_capture_reloads_a_fresh_bundle_after_buffer_expiry(monkeypatch) -> None:
    class FakeFabric:
        def __init__(self) -> None:
            self.bundle_requests = 0
            self.requests: list[str] = []

        async def latest_optional(self, stream: str) -> dict:
            self.requests.append(stream)
            if stream == "camera.calibration":
                return {
                    "data": {
                        "rgb_intrinsic": {
                            "fx": 100.0,
                            "fy": 100.0,
                            "cx": 0.0,
                            "cy": 0.0,
                        },
                        "calibration_revision": "test",
                    }
                }
            if stream == "localization.body.pose":
                return {
                    "data": {
                        "session_epoch": "epoch",
                        "world_frame": "vio",
                    }
                }
            if stream == "localization.vio.status":
                return {"data": {"session_epoch": "epoch"}}
            if stream == "camera.rgbd.bundle":
                self.bundle_requests += 1
                generation = self.bundle_requests
                common = {
                    "mapping_name": "camera-test",
                    "generation": generation,
                    "width": 1,
                    "height": 1,
                    "frame_number": generation,
                    "global_timestamp_us": generation * 100,
                }
                return {
                    "data": {
                        "rgb": {**common, "format_name": "RGB"},
                        "depth_aligned_to_rgb": {
                            **common,
                            "format_name": "Y16",
                            "depth_value_scale_mm": 1.0,
                        },
                    }
                }
            raise AssertionError(stream)

    class FakeReader:
        def open(self):
            return self

        def close(self) -> None:
            return None

        def read_ref(self, reference: dict) -> bytes:
            if reference["generation"] == 1:
                raise RuntimeError("BufferRef has expired or the slot was recycled")
            if reference["format_name"] == "RGB":
                return bytes([1, 2, 3])
            return bytes([232, 3])

    monkeypatch.setattr(camera_module, "CameraSharedMemory", lambda _: FakeReader())
    fabric = FakeFabric()

    captured = asyncio.run(
        RgbdCapture(fabric, "camera").capture(attempts=2, retry_delay_s=0)
    )

    assert captured.frame_number == 2
    assert captured.rgb.tolist() == [[[1, 2, 3]]]
    assert captured.depth_m[0, 0] == pytest.approx(1.0)
    assert captured.observations["capture"] == {
        "copy_attempt": 2,
        "transient_buffer_error_count": 1,
    }
    assert fabric.bundle_requests == 2
    assert fabric.requests[:3] == [
        "camera.calibration",
        "localization.body.pose",
        "localization.vio.status",
    ]


def test_capture_does_not_mislabel_decode_errors_as_buffer_expiry(
    monkeypatch,
) -> None:
    class FakeFabric:
        async def latest_optional(self, stream: str) -> dict:
            if stream == "camera.calibration":
                return {"data": {"rgb_intrinsic": {"fx": 1.0}}}
            if stream == "localization.body.pose":
                return {"data": {"session_epoch": "epoch", "world_frame": "vio"}}
            if stream == "localization.vio.status":
                return {"data": {}}
            return {
                "data": {
                    "rgb": {
                        "mapping_name": "camera-test",
                        "generation": 1,
                        "format_name": "UNKNOWN",
                        "width": 1,
                        "height": 1,
                    },
                    "depth_aligned_to_rgb": {
                        "mapping_name": "camera-test",
                        "generation": 1,
                        "format_name": "Y16",
                        "width": 1,
                        "height": 1,
                    },
                }
            }

    class FakeReader:
        def open(self):
            return self

        def close(self) -> None:
            return None

        def read_ref(self, _: dict) -> bytes:
            return b"\x00\x00\x00"

    monkeypatch.setattr(camera_module, "CameraSharedMemory", lambda _: FakeReader())

    with pytest.raises(RuntimeError, match="unsupported RGB format"):
        asyncio.run(
            RgbdCapture(FakeFabric(), "camera").capture(
                attempts=2,
                retry_delay_s=0,
            )
        )
