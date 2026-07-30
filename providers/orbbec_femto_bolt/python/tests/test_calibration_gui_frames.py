from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from orbbec_femto_provider.calibration_gui import CalibrationSession


class CalibrationGuiFrameTests(unittest.TestCase):
    def test_rgb_payload_is_encoded_as_jpeg(self) -> None:
        reference = SimpleNamespace(
            format_name="RGB",
            width=2,
            height=1,
        )

        encoded = CalibrationSession._encode_color(
            reference,
            bytes([255, 0, 0, 0, 255, 0]),
        )

        self.assertTrue(encoded.startswith(b"\xff\xd8"))

    def test_aligned_depth_payload_is_encoded_as_png(self) -> None:
        reference = SimpleNamespace(width=2, height=2)
        payload = np.asarray(
            [[0, 500], [1000, 1500]],
            dtype="<u2",
        ).tobytes()

        encoded = CalibrationSession._encode_depth(reference, payload)

        self.assertTrue(encoded.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
