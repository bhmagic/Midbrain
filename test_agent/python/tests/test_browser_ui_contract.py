from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import validate


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
            / "providers"
            / "foundation_pose"
            / "web"
            / "developer.css",
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
            root
            / "platform_core"
            / "manager"
            / "web"
            / "manager.css",
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "regular_agent.html",
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
            root
            / "platform_core"
            / "manager"
            / "web"
            / "manager.css",
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "regular_agent.html",
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

    def test_foundation_pose_browser_ui_matches_provider_duty(self) -> None:
        root = Path(__file__).resolve().parents[3]
        provider_root = root / "providers" / "foundation_pose"
        manifest = json.loads(
            (provider_root / "manifest.json").read_text(encoding="utf-8")
        )
        html = (provider_root / "web" / "developer.html").read_text(
            encoding="utf-8"
        )
        script = (provider_root / "web" / "developer.js").read_text(
            encoding="utf-8"
        )
        provider = (provider_root / "provider.py").read_text(encoding="utf-8")

        self.assertEqual(
            manifest["ui"]["developer"]["url_from_control"],
            "/dev",
        )
        self.assertEqual(
            manifest["ui"]["developer"]["availability"],
            "PROVIDER_RUNNING",
        )
        for expected in (
            "Latest camera-relative poses",
            "Installed CAD models",
            "Release unused GPU resources",
        ):
            self.assertIn(expected, html)
        for retired_workflow in (
            "Start Midbrain + Providers",
            "Ask VLM",
            "Make SAM2 Masks",
        ):
            self.assertNotIn(retired_workflow, html)
            self.assertNotIn(retired_workflow, script)
        self.assertIn('path in {"/dev", "/dev/"}', provider)
        self.assertIn('"latest_measurements"', provider)

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

    def test_manager_observation_surface_has_no_mutating_requests(self) -> None:
        root = Path(__file__).resolve().parents[3]
        component = (
            root
            / "platform_core"
            / "manager"
            / "web"
            / "component.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn('method: "POST"', component)
        self.assertNotIn("method: 'POST'", component)
        self.assertIn('cache: "no-store"', component)

    def test_mainframe_exposes_both_agent_profiles(self) -> None:
        root = Path(__file__).resolve().parents[3]
        mainframe = (
            root
            / "platform_core"
            / "manager"
            / "web"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="regularAgentLink"', mainframe)
        self.assertIn('id="developerAgentLink"', mainframe)
        self.assertIn('id="providerRows"', mainframe)
        self.assertIn('id="skillRows"', mainframe)

    def test_both_agent_surfaces_expose_model_controls(self) -> None:
        root = Path(__file__).resolve().parents[3]
        regular = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "regular_agent.html"
        ).read_text(encoding="utf-8")
        developer = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "app.py"
        ).read_text(encoding="utf-8")

        for surface in (regular, developer):
            self.assertIn('id="agentModel"', surface)
            self.assertIn('id="reasoningEffort"', surface)
            self.assertIn('id="vlmModel"', surface)
            self.assertIn("reasoning_effort", surface)
            self.assertIn("vlm_model", surface)

    def test_space_cognition_has_a_dedicated_development_surface(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")
        manifest = json.loads(
            (
                root / "skills" / "initialize_space_cognition" / "manifest.json"
            ).read_text(encoding="utf-8")
        )

        route = "/dev/skills/initialize-space-cognition"
        self.assertIn(f'"{route}"', app)
        self.assertIn('id="spaceCognitionLinkPanel"', app)
        self.assertIn('id="spaceCognitionPanel"', app)
        self.assertIn("spaceCognitionPanel.hidden = true", app)
        self.assertLess(
            app.index('id="worldPointCloudPanel"'),
            app.index('id="spaceCognitionLinkPanel"'),
        )
        self.assertTrue(manifest["ui"]["developer"]["url"].endswith(route))

    def test_developer_boundary_requires_explicit_overstep_confirmation(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        boundary = (
            root
            / "platform_core"
            / "manager"
            / "web"
            / "developer-confirm.html"
        ).read_text(encoding="utf-8")

        self.assertIn("overstepping the ordinary agent workflow", boundary)
        self.assertIn('id="confirmCheck"', boundary)
        self.assertIn('id="continueButton"', boundary)
        self.assertIn("disabled", boundary)

    def test_provider_ui_descriptors_are_schema_valid(self) -> None:
        root = Path(__file__).resolve().parents[3]
        schema = json.loads(
            (
                root
                / "contracts"
                / "schemas"
                / "component_ui.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        manifests = sorted((root / "providers").glob("*/manifest.json"))
        self.assertGreaterEqual(len(manifests), 5)
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn("ui", manifest, str(manifest_path))
            validate(instance=manifest["ui"], schema=schema)

    def test_declared_skill_ui_descriptors_are_schema_valid(self) -> None:
        root = Path(__file__).resolve().parents[3]
        schema = json.loads(
            (
                root
                / "contracts"
                / "schemas"
                / "component_ui.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        declared = 0
        for manifest_path in sorted((root / "skills").glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "ui" not in manifest:
                continue
            declared += 1
            validate(instance=manifest["ui"], schema=schema)
        self.assertGreaterEqual(declared, 2)

    def test_workspace_launcher_is_observation_first_and_detached(self) -> None:
        root = Path(__file__).resolve().parents[3]
        launcher = (
            root
            / "platform_core"
            / "scripts"
            / "run_workspace_bounded.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[switch]$AllowProviderAutoStart", launcher)
        self.assertIn(
            '"MANAGER_PROVIDER_AUTOSTART_ENABLED" = $providerAutoStartValue',
            launcher,
        )
        self.assertIn(
            '"AUTO_INITIALIZE_SPACE_COGNITION" = "false"',
            launcher,
        )
        self.assertIn("$startInfo.UseShellExecute = $true", launcher)
        self.assertIn(
            "$startInfo.WindowStyle = "
            "[System.Diagnostics.ProcessWindowStyle]::Hidden",
            launcher,
        )
        self.assertNotIn("$startInfo.RedirectStandardOutput = $true", launcher)
        self.assertNotIn("$startInfo.RedirectStandardError = $true", launcher)

    def test_desktop_entrypoints_use_the_bounded_workspace_scripts(self) -> None:
        root = Path(__file__).resolve().parents[3]
        start = (root / "Start Midbrain.cmd").read_text(
            encoding="utf-8"
        )
        stop = (root / "Stop Midbrain.cmd").read_text(encoding="utf-8")

        self.assertIn(
            "platform_core\\scripts\\run_workspace.ps1",
            start,
        )
        self.assertIn("-StartAgentUi", start)
        self.assertNotIn("-AllowProviderAutoStart", start)
        self.assertIn(
            "platform_core\\scripts\\stop_workspace.ps1",
            stop,
        )

    def test_main_ui_exposes_guarded_shutdown_and_activation(self) -> None:
        root = Path(__file__).resolve().parents[3]
        web = root / "platform_core" / "manager" / "web"
        main = (web / "index.html").read_text(encoding="utf-8")
        boundary = (web / "developer-confirm.js").read_text(
            encoding="utf-8"
        )
        shutdown = (web / "shutdown.html").read_text(encoding="utf-8")

        self.assertIn("<h1>Midbrain</h1>", main)
        self.assertIn('href="/shutdown"', main)
        self.assertIn("ACTIVATE_DEVELOPER_SURFACE", boundary)
        self.assertIn("Activating Provider through Manager", boundary)
        self.assertIn("within 30 seconds", boundary)
        self.assertIn('id="shutdownButton"', shutdown)
        self.assertIn("disabled", shutdown)
        self.assertIn(
            "platform_core/scripts/stop_workspace.ps1",
            shutdown,
        )

    def test_agent_approvals_are_presented_without_raw_json(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")
        regular = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "regular_agent.html"
        ).read_text(encoding="utf-8")

        self.assertIn("approval.title", app)
        self.assertIn("approval.warning", app)
        self.assertNotIn(
            "JSON.stringify(approval.request || {}, null, 2)",
            app,
        )
        self.assertIn("approval.title", regular)
        self.assertIn("/api/runs/", regular)

    def test_ui_shutdown_invokes_workspace_stop_script_directly(self) -> None:
        root = Path(__file__).resolve().parents[3]
        manager_ui = (
            root / "platform_core" / "manager" / "src" / "ui.rs"
        ).read_text(encoding="utf-8")

        self.assertIn('.join("stop_workspace.ps1")', manager_ui)
        self.assertNotIn('.join("stop_workspace_delayed.ps1")', manager_ui)


if __name__ == "__main__":
    unittest.main()
