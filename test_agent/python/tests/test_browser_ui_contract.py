from __future__ import annotations

import json
import unittest
from pathlib import Path


class BrowserUiContractTests(unittest.TestCase):
    def _browser_surfaces(self, root: Path) -> list[Path]:
        surfaces = [
            root
            / "providers"
            / "rebot_arm_integrated"
            / "python"
            / "rebot_arm_integrated"
            / "web"
            / "styles.css",
            root
            / "providers"
            / "rebot_arm_dm"
            / "python"
            / "rebot_arm_dm_provider"
            / "calibration_web"
            / "styles.css",
            root
            / "providers"
            / "orbbec_femto_bolt"
            / "python"
            / "orbbec_femto_provider"
            / "calibration_web"
            / "styles.css",
            root
            / "skills"
            / "stationary_world_arm_alignment"
            / "python"
            / "stationary_world_arm_alignment"
            / "web"
            / "style.css",
            root
            / "skills"
            / "vegetable_cutting"
            / "python"
            / "vegetable_cutting"
            / "web"
            / "style.css",
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "app.py",
        ]
        return [surface for surface in surfaces if surface.exists()]

    def test_browser_surfaces_expose_unified_theme_tokens(self) -> None:
        root = Path(__file__).resolve().parents[3]
        theme = json.loads(
            (root / "contracts" / "ui_theme.v1.json").read_text(
                encoding="utf-8"
            )
        )
        required_values = set(theme["tokens"].values())
        surfaces = [
            root
            / "providers"
            / "rebot_arm_integrated"
            / "python"
            / "rebot_arm_integrated"
            / "web"
            / "styles.css",
            root
            / "skills"
            / "stationary_world_arm_alignment"
            / "python"
            / "stationary_world_arm_alignment"
            / "web"
            / "style.css",
            root
            / "skills"
            / "vegetable_cutting"
            / "python"
            / "vegetable_cutting"
            / "web"
            / "style.css",
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "app.py",
        ]
        for surface in (path for path in surfaces if path.exists()):
            content = surface.read_text(encoding="utf-8")
            missing = sorted(
                value for value in required_values if value not in content
            )
            self.assertEqual(missing, [], str(surface))

    def test_all_browser_surfaces_use_neutral_ordinary_chrome(self) -> None:
        root = Path(__file__).resolve().parents[3]
        neutral_anchors = {"#090909", "#131313", "#f2f2f2"}
        retired_tinted_chrome = {
            "#07111b",
            "#081019",
            "#0a0f14",
            "#102033",
            "#47b7e8",
        }

        for surface in self._browser_surfaces(root):
            content = surface.read_text(encoding="utf-8").lower()
            self.assertEqual(
                sorted(color for color in neutral_anchors if color not in content),
                [],
                str(surface),
            )
            self.assertEqual(
                sorted(color for color in retired_tinted_chrome if color in content),
                [],
                str(surface),
            )

    def test_integrated_model_canvas_uses_neutral_background(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root
            / "providers"
            / "rebot_arm_integrated"
            / "python"
            / "rebot_arm_integrated"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn('context.fillStyle = "#0a0a0a"', app)
        self.assertNotIn('context.fillStyle = "#0a0f14"', app)

    def test_legacy_foundation_debug_ui_uses_neutral_chrome(self) -> None:
        root = Path(__file__).resolve().parents[3]
        gui = (
            root
            / "providers"
            / "foundation_pose"
            / "python"
            / "foundation_pose_provider"
            / "gui_app.py"
        ).read_text(encoding="utf-8")

        for color in ("#090909", "#131313", "#f2f2f2", "#3b3b3b"):
            self.assertIn(color, gui)
        for color in ("#07111b", "#081019", "#0a0f14", "#47b7e8"):
            self.assertNotIn(color, gui)

    def test_authorization_popup_states_approval_does_not_execute(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('id="authorizationDialog"', app)
        self.assertIn("Approval records this decision only", app)
        self.assertIn("approval_executes_action", app)

    def test_binding_validity_is_visible_in_development_ui(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('id="bindingState"', app)
        self.assertIn("binding.validation_issues", app)
        self.assertIn('"capability_binding": pointing_skill.last_binding', app)


if __name__ == "__main__":
    unittest.main()
