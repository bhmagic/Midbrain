from __future__ import annotations

import types
import unittest

import cv2
import numpy as np

from local_vio_provider.prototype_backend import (
    _FeatureSet,
    _FrameState,
    _circular_kernel,
    _circular_local_contrast_normalize,
    PrototypeRgbdImuOdometry,
)


class FeaturePreprocessingTests(unittest.TestCase):
    def test_circular_lcn_preserves_shape_and_increases_dim_local_contrast(self) -> None:
        image = np.full((120, 160), 24, dtype=np.uint8)
        cv2.rectangle(image, (25, 25), (135, 95), 31, thickness=2)
        cv2.line(image, (10, 110), (150, 10), 35, thickness=1)
        normalized = _circular_local_contrast_normalize(image, _circular_kernel(9))

        self.assertEqual(normalized.shape, image.shape)
        self.assertEqual(normalized.dtype, np.uint8)
        self.assertGreater(float(np.std(normalized)), float(np.std(image)))

    def test_adaptive_mode_always_keeps_raw_baseline(self) -> None:
        backend = PrototypeRgbdImuOdometry(feature_preprocess_mode="adaptive_circular_lcn")
        dim = np.full((100, 140), 30, dtype=np.uint8)
        cv2.circle(dim, (70, 50), 20, 45, thickness=2)
        features = backend._extract_feature_sets(dim)

        self.assertIn("RAW_BASELINE", features)
        self.assertIn("CIRCULAR_LCN", features)

    def test_lcn_must_materially_outperform_raw_before_selection(self) -> None:
        backend = PrototypeRgbdImuOdometry(lcn_selection_margin=0.12)
        backend.configure(np.eye(3), np.eye(4))
        features = {
            "RAW_BASELINE": _FeatureSet("RAW_BASELINE", [], np.ones((1, 32), dtype=np.uint8)),
            "CIRCULAR_LCN": _FeatureSet("CIRCULAR_LCN", [], np.ones((1, 32), dtype=np.uint8)),
        }
        state = _FrameState(0, np.zeros((2, 2), dtype=np.uint8), np.ones((2, 2)), features, 20.0)
        identity = np.eye(4)

        def close_results(self, previous, previous_features, current_features, **kwargs):
            if previous_features.name == "RAW_BASELINE":
                return identity, 40, 45, "VISUAL_EPNP", 1.0
            return identity, 44, 50, "VISUAL_EPNP", 1.0

        backend._estimate_step_for_features = types.MethodType(close_results, backend)
        result = backend._estimate_best_step(state, state)
        self.assertIsNotNone(result)
        self.assertEqual(result[3], "RAW_BASELINE")

        def stronger_lcn(self, previous, previous_features, current_features, **kwargs):
            if previous_features.name == "RAW_BASELINE":
                return identity, 40, 45, "VISUAL_EPNP", 1.0
            return identity, 52, 58, "VISUAL_EPNP", 1.0

        backend._estimate_step_for_features = types.MethodType(stronger_lcn, backend)
        result = backend._estimate_best_step(state, state)
        self.assertIsNotNone(result)
        self.assertEqual(result[3], "CIRCULAR_LCN_SELECTED")

    def test_healthy_raw_result_short_circuits_normalized_pnp(self) -> None:
        backend = PrototypeRgbdImuOdometry(lcn_raw_inlier_accept=70)
        backend.configure(np.eye(3), np.eye(4))
        features = {
            "RAW_BASELINE": _FeatureSet("RAW_BASELINE", [], np.ones((1, 32), dtype=np.uint8)),
            "CIRCULAR_LCN": _FeatureSet("CIRCULAR_LCN", [], np.ones((1, 32), dtype=np.uint8)),
        }
        state = _FrameState(0, np.zeros((2, 2), dtype=np.uint8), np.ones((2, 2)), features, 20.0)
        identity = np.eye(4)
        calls = []

        def fake_step(self, previous, previous_features, current_features, **kwargs):
            calls.append(previous_features.name)
            if previous_features.name == "RAW_BASELINE":
                return identity, 90, 100, "VISUAL_EPNP", 1.0
            raise AssertionError("normalized PnP should not run after a healthy raw result")

        backend._estimate_step_for_features = types.MethodType(fake_step, backend)
        result = backend._estimate_best_step(state, state)
        self.assertIsNotNone(result)
        self.assertEqual(result[3], "RAW_BASELINE")
        self.assertEqual(calls, ["RAW_BASELINE"])


if __name__ == "__main__":
    unittest.main()
