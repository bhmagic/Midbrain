from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from orbbec_femto_provider.shared_memory_access import BufferRef


class AlignedDepthValidityTests(unittest.TestCase):
    def _provider(self):
        provider_path = Path(__file__).resolve().parents[2] / "provider.py"
        spec = importlib.util.spec_from_file_location(
            "orbbec_femto_bolt_provider_entrypoint",
            provider_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load Orbbec Provider from {provider_path}")
        provider_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider_module)

        provider = object.__new__(provider_module.FemtoBoltProvider)
        provider.reader = Mock()
        return provider

    @staticmethod
    def _reference() -> BufferRef:
        return BufferRef(
            transport="windows_named_shared_memory",
            mapping_name=r"Local\Test",
            stream_kind=8,
            stream_name="depth_aligned_to_color",
            pool_id="pool",
            slot_id=0,
            generation=7,
            slot_offset=0,
            payload_offset=0,
            payload_bytes=48,
            payload_capacity_bytes=48,
            frame_number=42,
            host_qpc=1,
            device_timestamp_us=100,
            system_timestamp_us=110,
            global_timestamp_us=120,
            frame_type=0,
            format=0,
            format_name="Y16",
            width=4,
            height=4,
            stride_bytes=12,
            bytes_per_pixel=2,
            depth_value_scale_mm=1.0,
            flags=0,
            metadata_mask=0,
            frame_metadata={},
            note="aligned",
        )

    def test_validity_metadata_honors_stride_and_reports_boundary(self) -> None:
        provider = self._provider()
        values = np.zeros((4, 6), dtype="<u2")
        values[1:4, 1:3] = 1000
        provider.reader.read_ref.return_value = values.tobytes()

        result = provider._aligned_depth_validity_metadata(self._reference())

        self.assertEqual(result["validity_status"], "OBSERVED")
        self.assertEqual(
            result["valid_boundary"],
            {"x": 1, "y": 1, "width": 2, "height": 3},
        )
        self.assertEqual(result["valid_fraction"], 6 / 16)
        self.assertEqual(result["source_generation"], 7)
        self.assertEqual(result["source_frame_number"], 42)

    def test_no_valid_depth_is_reported_without_inventing_boundary(self) -> None:
        provider = self._provider()
        provider.reader.read_ref.return_value = np.zeros(
            (4, 6),
            dtype="<u2",
        ).tobytes()

        result = provider._aligned_depth_validity_metadata(self._reference())

        self.assertEqual(result["validity_status"], "NO_VALID_DEPTH")
        self.assertNotIn("valid_boundary", result)
        self.assertEqual(result["valid_fraction"], 0.0)


class RgbdPairingTests(unittest.TestCase):
    @staticmethod
    def _provider_module():
        provider_path = Path(__file__).resolve().parents[2] / "provider.py"
        spec = importlib.util.spec_from_file_location(
            "orbbec_femto_bolt_provider_pairing_test",
            provider_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load Orbbec Provider from {provider_path}")
        provider_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider_module)
        return provider_module

    @staticmethod
    def _reference(
        *,
        stream_kind: int,
        frame_number: int,
        generation: int,
        global_timestamp_us: int,
        system_timestamp_us: int,
    ) -> BufferRef:
        return BufferRef(
            transport="windows_named_shared_memory",
            mapping_name=r"Local\Test",
            stream_kind=stream_kind,
            stream_name=str(stream_kind),
            pool_id="pool",
            slot_id=generation,
            generation=generation,
            slot_offset=0,
            payload_offset=0,
            payload_bytes=2,
            payload_capacity_bytes=2,
            frame_number=frame_number,
            host_qpc=1,
            device_timestamp_us=global_timestamp_us - 100,
            system_timestamp_us=system_timestamp_us,
            global_timestamp_us=global_timestamp_us,
            frame_type=0,
            format=0,
            format_name="Y16",
            width=1,
            height=1,
            stride_bytes=2,
            bytes_per_pixel=2,
            depth_value_scale_mm=1.0,
            flags=0,
            metadata_mask=0,
            frame_metadata={},
            note="test",
        )

    def test_retained_pair_avoids_independent_latest_frame_race(self) -> None:
        provider_class = self._provider_module().FemtoBoltProvider
        matching_rgb = self._reference(
            stream_kind=0,
            frame_number=100,
            generation=1,
            global_timestamp_us=1_000_000,
            system_timestamp_us=1_000_000,
        )
        newer_unmatched_rgb = self._reference(
            stream_kind=0,
            frame_number=106,
            generation=2,
            global_timestamp_us=1_300_000,
            system_timestamp_us=1_300_000,
        )
        depth = self._reference(
            stream_kind=1,
            frame_number=94,
            generation=3,
            global_timestamp_us=1_001_000,
            system_timestamp_us=1_028_000,
        )

        pair = provider_class._newest_synchronized_pair(
            [matching_rgb, newer_unmatched_rgb],
            [depth],
            maximum_delta_us=50_000,
        )

        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertEqual(pair[0].frame_number, 100)
        self.assertEqual(pair[1].frame_number, 94)

    def test_registered_depth_prefers_exact_rgb_frame_when_retained(self) -> None:
        provider_class = self._provider_module().FemtoBoltProvider
        rgb = self._reference(
            stream_kind=0,
            frame_number=100,
            generation=1,
            global_timestamp_us=1_000_000,
            system_timestamp_us=1_000_000,
        )
        closer_wrong_frame = self._reference(
            stream_kind=8,
            frame_number=99,
            generation=2,
            global_timestamp_us=1_001_000,
            system_timestamp_us=1_300_000,
        )
        exact_frame = self._reference(
            stream_kind=8,
            frame_number=100,
            generation=3,
            global_timestamp_us=1_020_000,
            system_timestamp_us=1_320_000,
        )

        matched = provider_class._nearest_synchronized_ref(
            rgb,
            [closer_wrong_frame, exact_frame],
            maximum_delta_us=50_000,
        )

        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched.frame_number, 100)


class CalibrationPublicationTests(unittest.TestCase):
    def _provider(self):
        provider_path = Path(__file__).resolve().parents[2] / "provider.py"
        spec = importlib.util.spec_from_file_location(
            "orbbec_femto_bolt_provider_calibration_test",
            provider_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load Orbbec Provider from {provider_path}")
        provider_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider_module)

        provider = object.__new__(provider_module.FemtoBoltProvider)
        provider.args = SimpleNamespace(fabric_url="http://fabric.test")
        provider.http = Mock()
        provider.last_calibration = None
        provider.last_calibration_publish_monotonic = None
        return provider, provider_module

    def test_calibration_is_periodically_republished(self) -> None:
        provider, provider_module = self._provider()
        provider.last_calibration = "calibration-a"
        provider.last_calibration_publish_monotonic = 10.0

        self.assertFalse(
            provider._calibration_publish_due(
                "calibration-a",
                now_monotonic=10.5,
            )
        )
        self.assertTrue(
            provider._calibration_publish_due(
                "calibration-a",
                now_monotonic=(
                    10.0 + provider_module.CALIBRATION_REPUBLISH_INTERVAL_S
                ),
            )
        )
        self.assertTrue(
            provider._calibration_publish_due(
                "calibration-b",
                now_monotonic=10.5,
            )
        )

    def test_failed_fabric_batch_does_not_mark_calibration_published(self) -> None:
        provider, _ = self._provider()
        provider.http.post.return_value.raise_for_status.side_effect = RuntimeError(
            "Fabric unavailable"
        )

        with self.assertRaisesRegex(RuntimeError, "Fabric unavailable"):
            provider._publish_observation_batch(
                [{"stream": "camera.calibration"}],
                calibration_publication=("calibration-a", 12.0),
            )

        self.assertIsNone(provider.last_calibration)
        self.assertIsNone(provider.last_calibration_publish_monotonic)

    def test_successful_fabric_batch_commits_calibration_publication(self) -> None:
        provider, _ = self._provider()

        provider._publish_observation_batch(
            [{"stream": "camera.calibration"}],
            calibration_publication=("calibration-a", 12.0),
        )

        self.assertEqual(provider.last_calibration, "calibration-a")
        self.assertEqual(provider.last_calibration_publish_monotonic, 12.0)


if __name__ == "__main__":
    unittest.main()
