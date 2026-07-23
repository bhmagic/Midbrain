from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


INTEGRATED_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "rebot_arm_integrated_provider_entry",
    INTEGRATED_ROOT / "provider.py",
)
PROVIDER_ENTRY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROVIDER_ENTRY)


class ProviderCliTests(unittest.TestCase):
    def test_legacy_scene_argument_is_accepted_for_registration_compatibility(self):
        scene_path = str(INTEGRATED_ROOT / "config" / "scene.json")

        args = PROVIDER_ENTRY.parse_args(["--scene", scene_path])

        self.assertEqual(args.scene, scene_path)

    def test_physical_start_and_gui_termination_use_deterministic_paths(self):
        start_script = (INTEGRATED_ROOT / "scripts" / "start_physical_gui_test.ps1").read_text()
        stop_script = (INTEGRATED_ROOT / "scripts" / "stop_physical_gui_test.ps1").read_text()
        service = (INTEGRATED_ROOT / "python" / "rebot_arm_integrated" / "service.py").read_text()
        web = (INTEGRATED_ROOT / "python" / "rebot_arm_integrated" / "web" / "app.js").read_text()

        self.assertIn("$ProviderRoot = Split-Path -Parent $PSScriptRoot", start_script)
        self.assertIn('Join-Path $ProviderRoot "scripts\\stop_physical_gui_test.ps1"', start_script)
        self.assertNotIn('Join-Path $root "scripts\\stop_physical_gui_test.ps1"', start_script)
        self.assertIn('/v1/calibration/safe-home', stop_script)
        self.assertIn('if (-not $SafeHome.success)', stop_script)
        self.assertIn("self.provider_root.parents[1].resolve()", service)
        self.assertIn("LAUNCH_UNCONFIRMED", service)
        self.assertIn("Authoritative shutdown helper acknowledged", service)
        self.assertIn("-LaunchId", service)
        self.assertNotIn("window.confirm", web)


if __name__ == "__main__":
    unittest.main()
