from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from foundation_pose_provider.backend import (
    MockFoundationPoseBackend,
    NvLabsFoundationPoseBackend,
)
from foundation_pose_provider.model_registry import ObjectModel


class MockBackendTests(unittest.TestCase):
    def test_initialize_uses_masked_depth_median(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = ObjectModel(
                model_id="test",
                mesh_path=Path(directory) / "unused.obj",
                semantic_frame="object/test",
                mesh_from_semantic=np.eye(4),
                scale_to_m=1.0,
                symmetry={"type": "NONE"},
                enabled=True,
                revision="r1",
            )
            backend = MockFoundationPoseBackend()
            rgb = np.zeros((4, 4, 3), dtype=np.uint8)
            depth = np.ones((4, 4), dtype=np.float32) * 0.8
            depth[0, 0] = 1.2
            mask = np.zeros((4, 4), dtype=bool)
            mask[0:2, 0:2] = True
            result = backend.initialize("s1", model, rgb, depth, np.eye(3), mask)
            self.assertAlmostEqual(float(result.camera_from_mesh[2, 3]), 0.8, places=5)
            tracked = backend.track("s1", rgb, depth, np.eye(3))
            self.assertEqual(tracked.camera_from_mesh.shape, (4, 4))
            backend.reset("s1")
            with self.assertRaises(RuntimeError):
                backend.track("s1", rgb, depth, np.eye(3))


class PreparedEstimatorCacheTests(unittest.TestCase):
    @staticmethod
    def _model(mesh_path: Path, revision: str = "r1") -> ObjectModel:
        return ObjectModel(
            model_id="future-cad-model",
            mesh_path=mesh_path,
            semantic_frame="object/future",
            mesh_from_semantic=np.eye(4),
            scale_to_m=0.001,
            symmetry={"type": "NONE"},
            enabled=True,
            revision=revision,
        )

    def test_cache_key_changes_with_mesh_content_or_model_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text("v 0 0 0\n", encoding="utf-8")
            backend = NvLabsFoundationPoseBackend(root)
            first = backend._model_cache_key(self._model(mesh))
            changed_revision = backend._model_cache_key(self._model(mesh, "r2"))
            mesh.write_text("v 0 0 0\nv 1 0 0\n", encoding="utf-8")
            changed_mesh = backend._model_cache_key(self._model(mesh))
            self.assertNotEqual(first, changed_revision)
            self.assertNotEqual(first, changed_mesh)

    def test_reset_pools_prepared_estimator_for_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text("v 0 0 0\n", encoding="utf-8")
            model = self._model(mesh)
            backend = NvLabsFoundationPoseBackend(root, debug_dir=root / "debug")
            cache_key = backend._model_cache_key(model)
            estimator = SimpleNamespace(pose_last=object(), debug_dir="old")
            backend._estimators["first"] = estimator
            backend._model_ids["first"] = model.model_id
            backend._model_cache_keys["first"] = cache_key

            backend.reset("first")
            acquired, cache_hit = backend._build_estimator("second", model)

            self.assertTrue(cache_hit)
            self.assertIs(acquired, estimator)
            self.assertIsNone(estimator.pose_last)
            self.assertEqual(backend._model_ids["second"], model.model_id)

    def test_prepared_cache_is_bounded_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text("v 0 0 0\n", encoding="utf-8")
            model = self._model(mesh)
            backend = NvLabsFoundationPoseBackend(
                root, prepared_model_cache_size=1
            )
            for index, cache_key in enumerate(("first-key", "second-key")):
                session_id = f"session-{index}"
                backend._estimators[session_id] = SimpleNamespace(pose_last=object())
                backend._model_ids[session_id] = model.model_id
                backend._model_cache_keys[session_id] = cache_key
                backend.reset(session_id)
            self.assertEqual(set(backend._idle_estimators), {"second-key"})

            disabled = NvLabsFoundationPoseBackend(
                root, prepared_model_cache_size=0
            )
            disabled._estimators["session"] = SimpleNamespace(pose_last=object())
            disabled._model_ids["session"] = model.model_id
            disabled._model_cache_keys["session"] = "key"
            disabled.reset("session")
            self.assertFalse(disabled._idle_estimators)


if __name__ == "__main__":
    unittest.main()
