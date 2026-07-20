from __future__ import annotations

import asyncio
import unittest

import numpy as np

from physical_agent_test.world_point_cloud import PointChunk, WorldPointCloudAccumulator


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


class WorldPointCloudLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
