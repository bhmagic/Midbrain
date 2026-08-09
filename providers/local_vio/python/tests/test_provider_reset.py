from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from local_vio_provider.prototype_backend import PoseResult
from provider import LocalVioProvider
from orbbec_femto_provider.shared_memory_access import (
    STREAM_ALIGNED_DEPTH,
    STREAM_COLOR,
)


class ProviderResetTests(unittest.TestCase):
    def make_provider(
        self,
        pose_publication_mode: str = "fixed_rig_initial_hold",
    ) -> LocalVioProvider:
        return LocalVioProvider(
            SimpleNamespace(
                gravity_samples=80,
                gravity_std_limit_mps2=0.35,
                backend="inertial_first_rgbd_eskf",
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:2",
                pose_publication_mode=pose_publication_mode,
            )
        )

    @staticmethod
    def pose_result(
        world_from_camera: np.ndarray,
        *,
        timestamp_us: int,
    ) -> PoseResult:
        return PoseResult(
            timestamp_us=timestamp_us,
            world_from_camera=world_from_camera,
            velocity_world_mps=np.zeros(3),
            tracking_state="TRACKING",
            inlier_count=100,
            match_count=120,
            translation_step_m=0.0,
            rotation_step_rad=0.0,
            gravity_sample_count=80,
            gravity_std_mps2=0.01,
            gyro_delta_rad=0.0,
            gravity_tracking_sample_count=50,
            gravity_correction_applied=False,
            gravity_tilt_error_rad=0.0,
            gravity_direction_std_rad=0.0,
            gravity_stationary_duration_s=1.0,
            gravity_correction_mode="READY",
            gravity_adjustment_state="READY",
            gravity_gyro_rms_radps=0.0,
            gravity_gyro_p95_radps=0.0,
            gravity_gyro_noise_floor_radps=0.0,
            gravity_gyro_effective_limit_radps=0.012,
            rotation_source="VISUAL_INERTIAL_FUSION",
            rotation_disagreement_rad=0.0,
            gyro_rotation_sample_count=10,
            gyro_rotation_angle_rad=0.0,
            feature_preprocess_mode="RAW_BASELINE",
            raw_keypoint_count=100,
            normalized_keypoint_count=0,
            frame_luma_median=100.0,
            message=None,
            pose_update_mode="VISUAL_CORRECTION",
            visual_update_accepted=True,
        )


    def test_internal_timestamps_prefer_system_domain_for_video_and_imu(self) -> None:
        provider = self.make_provider()
        reference = {
            "global_timestamp_us": 900,
            "system_timestamp_us": 700,
            "device_timestamp_us": 500,
        }
        sample = SimpleNamespace(
            global_timestamp_us=901,
            system_timestamp_us=701,
            device_timestamp_us=501,
        )
        self.assertEqual(provider._reference_timestamp(reference), 700)
        self.assertEqual(provider._sample_timestamp(sample), 701)
        self.assertEqual(provider._visual_capture_timestamp(reference), 900)

    def test_session_reset_keeps_observation_sequence_monotonic(self) -> None:
        provider = self.make_provider()
        provider.sequence = 1234
        old_epoch = provider.session_epoch
        provider._reset_session("test")
        self.assertEqual(provider.sequence, 1234)
        self.assertNotEqual(provider.session_epoch, old_epoch)

    def test_fixed_rig_mode_holds_published_pose_while_reporting_live_drift(
        self,
    ) -> None:
        provider = self.make_provider()
        published_batches = []

        class _Response:
            def raise_for_status(self) -> None:
                return None

        class _Http:
            def post(self, _url, json):
                published_batches.append(json["observations"])
                return _Response()

        provider.http = _Http()
        first = np.eye(4, dtype=np.float64)
        moved = np.eye(4, dtype=np.float64)
        moved[:3, 3] = [0.25, -0.1, 0.05]

        provider._publish_result(self.pose_result(first, timestamp_us=1000))
        provider._publish_result(self.pose_result(moved, timestamp_us=2000))

        first_streams = {item["stream"]: item for item in published_batches[0]}
        second_streams = {item["stream"]: item for item in published_batches[1]}
        self.assertEqual(
            first_streams["localization.body.pose"]["data"]["world_from_camera"],
            second_streams["localization.body.pose"]["data"]["world_from_camera"],
        )
        self.assertEqual(
            first_streams["transform.local_vio.body"]["data"]["translation_m"],
            second_streams["transform.local_vio.body"]["data"]["translation_m"],
        )
        second_status = second_streams["localization.vio.status"]["data"]
        self.assertFalse(second_status["live_pose_exposed"])
        self.assertAlmostEqual(
            second_status["live_pose_drift_from_published_hold"]["translation_m"],
            float(np.linalg.norm(moved[:3, 3])),
        )
        self.assertEqual(
            second_streams["localization.body.pose"]["data"]["pose_update_mode"],
            "FIXED_RIG_INITIAL_HOLD",
        )

    def test_live_mode_keeps_publishing_estimator_motion(self) -> None:
        provider = self.make_provider("live_vio")
        first = np.eye(4, dtype=np.float64)
        moved = np.eye(4, dtype=np.float64)
        moved[0, 3] = 0.25

        selected_first = provider._select_published_pose(
            world_from_body=first,
            world_from_camera=first,
            world_from_camera_level=first,
            covariance=[0.0] * 36,
            timestamp_us=1000,
        )
        selected_moved = provider._select_published_pose(
            world_from_body=moved,
            world_from_camera=moved,
            world_from_camera_level=moved,
            covariance=[0.0] * 36,
            timestamp_us=2000,
        )

        self.assertAlmostEqual(selected_first[1][0, 3], 0.0)
        self.assertAlmostEqual(selected_moved[1][0, 3], 0.25)


    def test_degraded_gravity_pose_is_published_after_origin_exists(self) -> None:
        provider = self.make_provider()
        provider.origin_translation_adjustment = np.eye(4)
        provider.last_static_transform_epoch = provider.session_epoch

        captured = {}

        class _Response:
            def raise_for_status(self) -> None:
                return None

        class _Http:
            def post(self, url, json):
                captured["url"] = url
                captured["json"] = json
                return _Response()

        provider.http = _Http()
        result = PoseResult(
            timestamp_us=123456,
            world_from_camera=np.eye(4),
            velocity_world_mps=np.zeros(3),
            tracking_state="DEGRADED",
            inlier_count=0,
            match_count=0,
            translation_step_m=0.0,
            rotation_step_rad=0.0,
            gravity_sample_count=80,
            gravity_std_mps2=0.01,
            gyro_delta_rad=0.0,
            gravity_tracking_sample_count=50,
            gravity_correction_applied=True,
            gravity_tilt_error_rad=0.02,
            gravity_direction_std_rad=0.01,
            gravity_stationary_duration_s=1.5,
            gravity_correction_mode="RECOVERY_ACTIVE",
            gravity_adjustment_state="ACTIVE",
            gravity_gyro_rms_radps=0.001,
            gravity_gyro_p95_radps=0.002,
            gravity_gyro_noise_floor_radps=0.001,
            gravity_gyro_effective_limit_radps=0.012,
            rotation_source="GYRO_PROPAGATION",
            rotation_disagreement_rad=0.1,
            gyro_rotation_sample_count=12,
            gyro_rotation_angle_rad=0.05,
            feature_preprocess_mode="RAW_BASELINE",
            raw_keypoint_count=100,
            normalized_keypoint_count=0,
            frame_luma_median=80.0,
            message="visual tracking unavailable",
        )

        provider._publish_result(result)
        streams = {item["stream"] for item in captured["json"]["observations"]}
        self.assertIn("localization.body.pose", streams)
        self.assertIn("transform.local_vio.body", streams)

    def test_reset_control_succeeds_when_immediate_status_publish_fails(self) -> None:
        provider = self.make_provider()
        provider._publish_status = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("transient Fabric failure")
        )
        old_epoch = provider.session_epoch
        result = provider.handle_request({"action": "initialize", "request_id": "test"})
        self.assertEqual(result["status"], "reset")
        self.assertNotEqual(result["session_epoch"], old_epoch)
        self.assertIn("status_publish_warning", result)

    def test_hot_request_is_idempotent_when_already_hot(self) -> None:
        provider = self.make_provider()
        provider.residency = "HOT"
        provider._heartbeat = lambda: None
        old_epoch = provider.session_epoch
        result = provider.start_hot()
        self.assertEqual(result["status"], "already_hot")
        self.assertEqual(provider.session_epoch, old_epoch)

    def test_fixed_rig_attestation_is_bounded_and_does_not_reset(self) -> None:
        provider = self.make_provider()
        old_epoch = provider.session_epoch

        result = provider.handle_request(
            {
                "action": "attest_fixed_rig_stationary",
                "related_skill_id": "skill-1",
                "payload": {
                    "fixed_rig_confirmed": True,
                    "duration_s": 10.0,
                },
            }
        )

        self.assertEqual(
            result["status"],
            "fixed_rig_stationary_attested",
        )
        self.assertFalse(result["epoch_reset"])
        self.assertEqual(provider.session_epoch, old_epoch)
        self.assertTrue(provider._fixed_rig_attestation_active())
        self.assertEqual(
            provider.fixed_rig_attestation_skill_id,
            "skill-1",
        )

    def test_fixed_rig_attestation_requires_explicit_confirmation(self) -> None:
        provider = self.make_provider()

        with self.assertRaisesRegex(
            ValueError,
            "fixed_rig_confirmed=true",
        ):
            provider.handle_request(
                {
                    "action": "attest_fixed_rig_stationary",
                    "payload": {"duration_s": 10.0},
                }
            )

    def test_session_reset_clears_fixed_rig_attestation(self) -> None:
        provider = self.make_provider()
        provider.handle_request(
            {
                "action": "attest_fixed_rig_stationary",
                "payload": {
                    "fixed_rig_confirmed": True,
                    "duration_s": 10.0,
                },
            }
        )

        provider._reset_session("test")

        self.assertFalse(provider._fixed_rig_attestation_active())
        self.assertIsNone(provider.fixed_rig_attestation_skill_id)
        self.assertIsNone(provider.held_world_from_camera)
        self.assertIsNone(provider.held_pose_timestamp_us)

    def test_rgbd_copy_matches_retained_capture_pair_not_independent_latest_refs(self) -> None:
        provider = object.__new__(LocalVioProvider)
        requested_streams = []

        class Reference:
            def __init__(
                self,
                label: str,
                frame_number: int,
                global_timestamp_us: int,
                system_timestamp_us: int,
            ):
                self.label = label
                self.frame_number = frame_number
                self.global_timestamp_us = global_timestamp_us
                self.system_timestamp_us = system_timestamp_us

            def to_dict(self) -> dict:
                return {
                    "label": self.label,
                    "frame_number": self.frame_number,
                    "global_timestamp_us": self.global_timestamp_us,
                    "system_timestamp_us": self.system_timestamp_us,
                }

        class Reader:
            def recent_refs(self, stream_kind: int):
                requested_streams.append(stream_kind)
                if stream_kind == STREAM_COLOR:
                    return [
                        Reference("rgb-matched", 100, 1_001_000, 1_001_000),
                        Reference("rgb-latest", 106, 1_300_000, 1_300_000),
                    ]
                if stream_kind == STREAM_ALIGNED_DEPTH:
                    return [
                        Reference("aligned", 100, 1_000_000, 1_300_000)
                    ]
                return []

        provider.reader = Reader()
        provider._read_depth_m = lambda reference: np.ones(
            (2, 2),
            dtype=np.float32,
        )
        provider._read_rgb = lambda reference: np.ones(
            (2, 2, 3),
            dtype=np.uint8,
        )

        rgb_ref, aligned_ref, rgb, depth = provider._read_latest_rgbd(
            maximum_delta_us=50_000
        )

        self.assertEqual(
            requested_streams,
            [STREAM_COLOR, STREAM_ALIGNED_DEPTH],
        )
        self.assertEqual(rgb_ref["label"], "rgb-matched")
        self.assertEqual(aligned_ref["label"], "aligned")
        self.assertEqual(rgb.shape, (2, 2, 3))
        self.assertEqual(depth.shape, (2, 2))


if __name__ == "__main__":
    unittest.main()
