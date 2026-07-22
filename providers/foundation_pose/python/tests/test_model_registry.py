from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from foundation_pose_provider.model_registry import ObjectModelRegistry


class ModelRegistryTests(unittest.TestCase):
    def test_registry_loads_relative_mesh_and_transform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text("o test\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            registry_path = root / "models.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "revision": "r1",
                        "models": [
                            {
                                "model_id": "arm_root",
                                "mesh_path": "model.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 0.001,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = ObjectModelRegistry(registry_path)
            model = registry.get("arm_root")
            self.assertEqual(model.mesh_path, mesh.resolve())
            self.assertEqual(model.semantic_frame, "robot/arm_root")
            self.assertEqual(model.scale_to_m, 0.001)
            self.assertEqual(registry.revision, "r1")

    def test_registry_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text(
                "o test\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                encoding="utf-8",
            )
            registry_path = root / "models.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "revision": "bom",
                        "models": [
                            {
                                "model_id": "arm_root",
                                "mesh_path": "model.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 0.001,
                            }
                        ],
                    }
                ),
                encoding="utf-8-sig",
            )
            registry = ObjectModelRegistry(registry_path)
            self.assertEqual(registry.revision, "bom")
            self.assertEqual(registry.get("arm_root").mesh_path, mesh.resolve())

    def test_missing_mesh_rejected_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "models.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "missing",
                                "mesh_path": "missing.obj",
                                "semantic_frame": "object/missing",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = ObjectModelRegistry(registry_path)
            with self.assertRaises(FileNotFoundError):
                registry.get("missing")
            self.assertEqual(registry.get("missing", require_mesh=False).model_id, "missing")


    def test_registry_exposes_role_and_default_child_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "model.obj"
            mesh.write_text(
                "o test\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                encoding="utf-8",
            )
            registry_path = root / "models.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "revision": "roles",
                        "models": [
                            {
                                "model_id": "robot_base",
                                "role": "robot_base",
                                "description": "Rigid base reporter",
                                "default_child_frame": "observed_object/robot/base",
                                "mesh_path": "model.obj",
                                "semantic_frame": "robot/base",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            registry = ObjectModelRegistry(registry_path)
            model = registry.get("robot_base")

            self.assertEqual(model.role, "robot_base")
            self.assertEqual(model.description, "Rigid base reporter")
            self.assertEqual(
                model.default_child_frame,
                "observed_object/robot/base",
            )

            public = model.public_payload()
            self.assertEqual(public["role"], "robot_base")
            self.assertEqual(
                public["default_child_frame"],
                "observed_object/robot/base",
            )


if __name__ == "__main__":
    unittest.main()
