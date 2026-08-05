from __future__ import annotations

import asyncio
import unittest

import numpy as np

from physical_agent_test.world_point_cloud import (
    CALIBRATION_ACTIVATION_STREAM,
    CAMERA_OPTICAL_CONVENTION_ID,
    LIVE_VIO_CAMERA_AUTHORITY,
    PointChunk,
    REVIEWED_STATIONARY_CAMERA_AUTHORITY,
    WORLD_CONVENTION_ID,
    WorldPointCloudAccumulator,
    _reviewed_stationary_camera_transform,
)


def _activation_observation() -> dict:
    return {
        "schema": "physical_agent.workcell_calibration_activation",
        "schema_version": 1,
        "stream": CALIBRATION_ACTIVATION_STREAM,
        "provider_id": "manager.workcell_calibration",
        "calibration_revision": "alignment-1",
        "expires_at_us": None,
        "valid": True,
        "data": {
            "activation_id": "activation-1",
            "calibration_revision": "alignment-1",
            "expires_at_us": None,
            "validity_policy": "MOUNTED_IDENTITY_TRACKING_GATED_V1",
            "state": "ACTIVE",
            "motion_usable": True,
            "session_epoch": "epoch-1",
            "vio_world_frame": "local_vio/epoch-1",
            "camera_frame": "femto_bolt_color_optical_frame",
            "convention_id": WORLD_CONVENTION_ID,
            "camera_optical_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
            "transforms": {
                "world_from_camera": {
                    "translation_m": [4.0, 6.0, 0.0],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                },
                "world_from_vio": {
                    "translation_m": [1.0, 2.0, 0.0],
                    "rotation_xyzw": [
                        0.0,
                        0.0,
                        2.0**-0.5,
                        2.0**-0.5,
                    ],
                },
            },
        },
    }


class _UnusedFabric:
    async def latest_optional(self, _stream):
        return None


class _DegradedFabric:
    async def latest_optional(self, stream):
        values = {
            "camera.rgbd.bundle": {"data": {}},
            "camera.calibration": {"data": {}},
            "localization.body.pose": {
                "data": {
                    "world_frame": "local_vio/new",
                    "session_epoch": "new",
                }
            },
            "localization.vio.status": {
                "data": {
                    "tracking_state": "DEGRADED",
                    "session_epoch": "new",
                }
            },
        }
        return values.get(stream)


class _CaptureFabric:
    def __init__(self, activation: dict) -> None:
        self.transform_calls = 0
        self.values = {
            "camera.rgbd.bundle": {
                "data": {
                    "coordinate_conventions": {
                        "rgb": CAMERA_OPTICAL_CONVENTION_ID,
                        "aligned_depth": CAMERA_OPTICAL_CONVENTION_ID,
                    },
                    "rgb": {
                        "frame_number": 1,
                        "mapping_name": "unused-test-mapping",
                        "global_timestamp_us": 1_000_000,
                    },
                    "depth_aligned_to_rgb": {},
                }
            },
            "camera.calibration": {
                "data": {
                    "rgb_intrinsic": {
                        "fx": 1.0,
                        "fy": 1.0,
                        "cx": 0.0,
                        "cy": 0.0,
                    }
                }
            },
            "localization.body.pose": {
                "data": {
                    "world_frame": "local_vio/epoch-1",
                    "session_epoch": "epoch-1",
                }
            },
            "localization.vio.status": {
                "data": {
                    "tracking_state": "TRACKING",
                    "world_frame": "local_vio/epoch-1",
                    "session_epoch": "epoch-1",
                    "convention_id": WORLD_CONVENTION_ID,
                }
            },
            CALIBRATION_ACTIVATION_STREAM: activation,
        }

    async def latest_optional(self, stream):
        return self.values.get(stream)

    async def transform(self, **_arguments):
        self.transform_calls += 1
        return {
            "translation_m": [7.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        }


class WorldPointCloudLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewed_stationary_camera_transform_is_expressed_in_vio_world(
        self,
    ) -> None:
        transform = _reviewed_stationary_camera_transform(
            _activation_observation(),
            session_epoch="epoch-1",
            vio_world_frame="local_vio/epoch-1",
            now_us=1_000_000,
        )

        self.assertIsNotNone(transform)
        assert transform is not None
        np.testing.assert_allclose(
            transform.vio_from_camera,
            np.array(
                [
                    [0.0, 1.0, 0.0, 4.0],
                    [-1.0, 0.0, 0.0, -3.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            ),
            atol=1e-12,
        )
        self.assertEqual(transform.calibration_revision, "alignment-1")
        self.assertEqual(transform.activation_id, "activation-1")

    async def test_reviewed_stationary_camera_transform_rejects_invalid_or_mismatched(
        self,
    ) -> None:
        legacy_timed = _activation_observation()
        legacy_timed["expires_at_us"] = 999_999
        legacy_timed["data"]["expires_at_us"] = 999_999
        legacy_timed["data"].pop("validity_policy")
        self.assertIsNone(
            _reviewed_stationary_camera_transform(
                legacy_timed,
                session_epoch="epoch-1",
                vio_world_frame="local_vio/epoch-1",
                now_us=1_000_000,
            )
        )

        mismatched_session = _activation_observation()
        mismatched_session["data"]["session_epoch"] = "epoch-old"
        self.assertIsNone(
            _reviewed_stationary_camera_transform(
                mismatched_session,
                session_epoch="epoch-1",
                vio_world_frame="local_vio/epoch-1",
                now_us=1_000_000,
            )
        )

        revoked = _activation_observation()
        revoked["data"]["state"] = "REVOKED"
        revoked["data"]["motion_usable"] = False
        self.assertIsNone(
            _reviewed_stationary_camera_transform(
                revoked,
                session_epoch="epoch-1",
                vio_world_frame="local_vio/epoch-1",
                now_us=1_000_000,
            )
        )

    async def test_transform_authority_change_clears_mixed_frame_history(self) -> None:
        accumulator = WorldPointCloudAccumulator(
            _UnusedFabric(),
            retention_s=10.0,
            sample_stride=4,
            update_hz=3.0,
            max_points=100_000,
        )
        accumulator.transform_authority = LIVE_VIO_CAMERA_AUTHORITY
        accumulator.last_frame_number = 12
        accumulator.last_success_monotonic = 1.0
        accumulator.chunks.append(PointChunk(1.0, np.zeros((3, 6), dtype=np.float32)))

        await accumulator._set_transform_authority(
            REVIEWED_STATIONARY_CAMERA_AUTHORITY,
            calibration_revision="alignment-1",
            activation_id="activation-1",
        )

        self.assertEqual(len(accumulator.chunks), 0)
        self.assertEqual(accumulator.last_frame_number, -1)
        self.assertIsNone(accumulator.last_success_monotonic)
        status = await accumulator.status()
        self.assertEqual(
            status["transform_authority"],
            REVIEWED_STATIONARY_CAMERA_AUTHORITY,
        )
        self.assertEqual(status["calibration_revision"], "alignment-1")
        self.assertEqual(status["calibration_activation_id"], "activation-1")

        accumulator.chunks.append(PointChunk(2.0, np.zeros((1, 6), dtype=np.float32)))
        await accumulator._set_transform_authority(
            REVIEWED_STATIONARY_CAMERA_AUTHORITY,
            calibration_revision="alignment-1",
            activation_id="activation-1",
        )
        self.assertEqual(len(accumulator.chunks), 1)

    async def test_point_projection_accepts_exact_reviewed_matrix(self) -> None:
        accumulator = WorldPointCloudAccumulator(
            _UnusedFabric(),
            retention_s=10.0,
            sample_stride=2,
            update_hz=3.0,
            max_points=100_000,
        )
        rgb = np.array([[[255, 128, 0]]], dtype=np.uint8)
        depth_m = np.array([[1.0]], dtype=np.float32)
        transform = np.eye(4, dtype=np.float64)
        transform[:3, 3] = [1.0, 2.0, 3.0]

        points = accumulator._make_world_points(
            rgb,
            depth_m,
            {"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0},
            transform,
        )

        np.testing.assert_allclose(points[0, :3], [1.0, 2.0, 4.0])
        np.testing.assert_allclose(points[0, 3:], [1.0, 128.0 / 255.0, 0.0])

    async def test_capture_uses_reviewed_reference_and_invalid_activation_falls_back(
        self,
    ) -> None:
        invalidated = _activation_observation()
        invalidated["data"]["state"] = "INVALIDATED"
        invalidated["data"]["motion_usable"] = False
        revoked = _activation_observation()
        revoked["data"]["state"] = "REVOKED"
        revoked["data"]["motion_usable"] = False
        mismatched = _activation_observation()
        mismatched["data"]["session_epoch"] = "epoch-old"

        for label, invalid_activation in (
            ("invalidated", invalidated),
            ("revoked", revoked),
            ("session mismatch", mismatched),
        ):
            with self.subTest(label=label):
                fabric = _CaptureFabric(
                    _activation_observation()
                )
                accumulator = WorldPointCloudAccumulator(
                    fabric,
                    retention_s=10.0,
                    sample_stride=2,
                    update_hz=3.0,
                    max_points=100_000,
                )
                accumulator._ensure_reader = lambda _mapping_name: None
                accumulator._read_rgb = lambda _reference: np.array(
                    [[[255, 0, 0]]], dtype=np.uint8
                )
                accumulator._read_depth_m = lambda _reference: np.array(
                    [[1.0]], dtype=np.float32
                )

                await accumulator._capture_once()

                self.assertEqual(fabric.transform_calls, 0)
                self.assertEqual(
                    accumulator.transform_authority,
                    REVIEWED_STATIONARY_CAMERA_AUTHORITY,
                )
                np.testing.assert_allclose(
                    accumulator.chunks[-1].points_xyzrgb[0, :3],
                    [4.0, -3.0, 1.0],
                    atol=1e-6,
                )

                fabric.values[CALIBRATION_ACTIVATION_STREAM] = invalid_activation
                fabric.values["camera.rgbd.bundle"]["data"]["rgb"][
                    "frame_number"
                ] = 2
                await accumulator._capture_once()

                self.assertEqual(fabric.transform_calls, 1)
                self.assertEqual(
                    accumulator.transform_authority,
                    LIVE_VIO_CAMERA_AUTHORITY,
                )
                self.assertEqual(len(accumulator.chunks), 1)
                np.testing.assert_allclose(
                    accumulator.chunks[-1].points_xyzrgb[0, :3],
                    [7.0, 0.0, 1.0],
                    atol=1e-6,
                )

    async def test_force_reinitialization_switches_and_resumes(self) -> None:
        accumulator = WorldPointCloudAccumulator(
            _UnusedFabric(),
            retention_s=10.0,
            sample_stride=4,
            update_hz=3.0,
            max_points=100_000,
        )
        accumulator.session_epoch = "old"
        accumulator.world_frame = "local_vio/old"
        accumulator.last_frame_number = 42
        accumulator.chunks.append(PointChunk(1.0, np.zeros((3, 6), dtype=np.float32)))

        await accumulator.begin_reinitialization()
        self.assertTrue(accumulator.suspended)
        self.assertEqual(len(accumulator.chunks), 1)

        await accumulator.switch_session(
            session_epoch="new",
            world_frame="local_vio/new",
        )
        self.assertFalse(accumulator.suspended)
        self.assertEqual(accumulator.session_epoch, "new")
        self.assertEqual(accumulator.world_frame, "local_vio/new")
        self.assertEqual(accumulator.last_frame_number, -1)
        self.assertEqual(len(accumulator.chunks), 0)
        await accumulator.stop()


    async def test_mapping_pauses_during_gravity_only_degraded_pose(self) -> None:
        accumulator = WorldPointCloudAccumulator(
            _DegradedFabric(),
            retention_s=10.0,
            sample_stride=4,
            update_hz=3.0,
            max_points=100_000,
        )
        await accumulator._capture_once()
        self.assertEqual(accumulator.capture_state, "PAUSED_UNTIL_VISUAL_TRACKING")

    async def test_transient_buffer_error_is_classified(self) -> None:
        self.assertTrue(
            WorldPointCloudAccumulator._is_transient_buffer_error(
                "BufferRef has expired or the slot was recycled"
            )
        )
        self.assertFalse(
            WorldPointCloudAccumulator._is_transient_buffer_error("transform unavailable")
        )


if __name__ == "__main__":
    unittest.main()
