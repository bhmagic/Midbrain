from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foundation_pose_provider.nvlabs_compat import (
    PATCH_MARKER,
    is_windows_temp_patch_applied,
    patch_windows_temp_path,
    verify_windows_temp_path,
)


UPSTREAM_FIXTURE = """from Utils import *\nimport yaml\n\nclass FoundationPose:\n  def reset_object(self):\n    self.mesh_path = None\n    self.mesh = mesh\n    if self.mesh is not None:\n      self.mesh_path = f'/tmp/{uuid.uuid4()}.obj'\n      self.mesh.export(self.mesh_path)\n"""


class NvLabsCompatibilityTests(unittest.TestCase):
    def test_windows_temp_patch_is_idempotent_and_portable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            estimater = root / "estimater.py"
            estimater.write_text(UPSTREAM_FIXTURE, encoding="utf-8")

            first = patch_windows_temp_path(root)
            self.assertEqual(first.status, "patched")
            self.assertTrue(is_windows_temp_patch_applied(root))

            text = estimater.read_text(encoding="utf-8")
            self.assertIn(PATCH_MARKER, text)
            self.assertIn("import tempfile", text)
            self.assertIn("tempfile.gettempdir()", text)
            self.assertIn("FOUNDATIONPOSE_TEMP_DIR", text)
            self.assertNotIn("self.mesh_path = f'/tmp/", text)

            second = patch_windows_temp_path(root)
            self.assertEqual(second.status, "already_patched")
            self.assertEqual(verify_windows_temp_path(root).status, "verified")

    def test_patch_refuses_unrecognized_upstream_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "estimater.py").write_text(
                "import yaml\n# upstream changed\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                patch_windows_temp_path(root)


if __name__ == "__main__":
    unittest.main()
