from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
