from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import validate


class BrowserUiContractTests(unittest.TestCase):
    def test_slicing_numeric_ui_exposes_two_gated_stages(self) -> None:
        root = Path(__file__).resolve().parents[3]
        page = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "slicing_developer.html"
        ).read_text(encoding="utf-8")
        manifest = json.loads(
            (root / "skills" / "slicing" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("RELATIVE_TO_CURRENT_EFFECTOR_WORLD", page)
        self.assertIn("Execute Stage 1", page)
        self.assertIn("Execute Stage 2", page)
        self.assertIn("current controlled-effector origin captured", page)
        self.assertIn("without collision planning", page)
        self.assertIn("/api/skills/slicing/development/prepare", page)
        self.assertIn("/api/skills/slicing/development/blade-profiles", page)
        self.assertIn("/api/skills/slicing/development/motion-profiles", page)
        self.assertIn("slice_length_m", page)
        self.assertIn("slice_wait_speed_m_s", page)
        self.assertIn("integrated_execution_backend", page)
        self.assertIn("POS_SPEED (Basic POSITION_VELOCITY_LIMITED)", page)
        self.assertIn("The next Agent invocation can use it.", page)
        self.assertNotIn(
            "Restart the workspace before Agent profile selection uses it.",
            page,
        )
        self.assertEqual(
            manifest["ui"]["developer"]["url"],
            "http://127.0.0.1:8000/dev/skills/slicing",
        )

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

    def test_integrated_developer_ui_is_read_only_except_safe_controls(self) -> None:
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

        self.assertIn('api("/v1/state"', app)
        self.assertIn('api("/v1/float"', app)
        self.assertIn('api("/v1/safe-terminate"', app)
        for retired_path in (
            "/v1/engage",
            "/v1/teleop",
            "/v1/settings",
            "/v1/gripper",
            "/v1/contact-baseline",
        ):
            self.assertNotIn(retired_path, app)

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

    def test_binding_validity_stays_in_status_but_not_prompt_chrome(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('id="bindingState"', app)
        self.assertNotIn("Camera capability binding", app)
        self.assertNotIn("binding.validation_issues", app)
        self.assertIn('"capability_binding": pointing_skill.last_binding', app)

    def test_developer_prompt_begins_empty(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '<textarea id="prompt" placeholder="Describe the task for the agent."></textarea>',
            app,
        )
        self.assertNotIn("Take a screenshot and identify the object", app)

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

    def test_mainframe_exposes_agent_profiles_and_run_journal(self) -> None:
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
        self.assertIn('id="runJournalLink"', mainframe)
        self.assertIn('id="providerRows"', mainframe)
        self.assertIn('id="skillRows"', mainframe)

    def test_run_journal_is_read_only_and_two_level_expandable(self) -> None:
        root = Path(__file__).resolve().parents[3]
        web_root = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
        )
        page = (web_root / "run_journal.html").read_text(encoding="utf-8")
        script = (web_root / "run_journal.js").read_text(encoding="utf-8")
        regular = (web_root / "regular_agent.html").read_text(
            encoding="utf-8"
        )
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)', page)
        self.assertIn('id="recordList"', page)
        self.assertIn("Midbrain sessions", page)
        self.assertIn('id="eventGroups"', page)
        self.assertIn('className = "session-card"', script)
        self.assertIn('className = "record-card"', script)
        self.assertIn("runsBySessionId: new Map()", script)
        self.assertIn("detailsByRunId: new Map()", script)
        self.assertIn('className = "event-group"', script)
        self.assertIn('className = "event-record"', script)
        self.assertIn("JSON.stringify(event, null, 2)", script)
        self.assertIn('/api/run-journal/sessions?limit=100', script)
        self.assertIn('/api/run-journal/sessions/${encodeURIComponent(sessionId)}', script)
        self.assertIn('/api/run-journal/runs/${encodeURIComponent(runId)}', script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn('method: "POST"', script)
        self.assertIn('href="/dev/run-journal"', regular)
        self.assertIn('@app.get("/dev/run-journal"', app)
        self.assertIn('@app.get("/api/run-journal/sessions")', app)
        self.assertIn('@app.get("/api/run-journal/sessions/{session_id}")', app)
        self.assertIn('@app.get("/api/run-journal/runs/{run_id}")', app)
        self.assertNotIn('@app.post("/api/run-journal', app)

    def test_developer_agent_uses_split_collapsible_workspace(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")
        history = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "agent_chat_history.js"
        ).read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", app)
        self.assertIn("developerDiagnosticsPane", app)
        self.assertIn("developerConversationPane", app)
        self.assertIn("collapsibleDiagnostic", app)
        self.assertIn("modernizeDeveloperWorkspace", app)
        self.assertIn("developer-chat-scroll", app)
        self.assertIn("detailedEvents: true", app)
        self.assertIn("turn.addEvent(event)", app)
        self.assertIn('className = "chat-event-details"', history)
        self.assertIn('className = "chat-event-record"', history)

    def test_world_viewer_exposes_semantic_annotation_layer(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('@app.get("/api/world-annotations")', app)
        self.assertIn("item_locator_skill.last_metric_result", app)
        self.assertIn('"LAST_TRUSTED_METRIC_ITEM_RESULT"', app)
        self.assertIn('id="annotationStats"', app)
        self.assertIn('id="showAnnotations"', app)
        self.assertIn('id="showKeepOut"', app)
        self.assertIn('id="showPushable"', app)
        self.assertIn('id="showWorkObject"', app)
        self.assertIn('id="showGripper"', app)
        self.assertIn('"type": "GRIPPER"', app)
        self.assertIn('"ACTIVE_EFFECTOR_PROFILE"', app)
        self.assertIn('markers.pop("robot-gripper-tool", None)', app)
        self.assertIn('"robot-effector-collider:', app)
        self.assertIn('"FABRIC_TRANSFORM"', app)
        self.assertIn("_frame_transform_to_world", app)
        self.assertIn(
            "VIO_WORLD_WITH_INDEPENDENT_ARM_CONTROL_EPOCH",
            app,
        )
        self.assertIn("annotationVisibilityControls", app)
        self.assertIn("rebuildWorldAnnotationBuffers", app)
        self.assertIn("refreshWorldAnnotations", app)
        self.assertIn('sphere.get("sphere_id")', app)
        self.assertIn('"visualization_limit": 240', app)
        self.assertIn('"boxes": list(boxes.values())', app)
        self.assertIn("function boxLineVertices(corners)", app)
        self.assertIn(
            "syncAnnotationLabels([...worldAnnotationMarkers, ...worldAnnotationBoxes])",
            app,
        )
        self.assertIn('"show_label": False', app)
        self.assertIn("scene display ${displayedSceneCount}/${sourceSceneCount}", app)
        self.assertIn(
            "Only user-declared KEEP_OUT geometry is blocking",
            app,
        )

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
            self.assertLess(
                surface.index('<textarea id="prompt"'),
                surface.index('<div class="model-controls"'),
            )
            self.assertIn("addEventListener(\"keydown\"", surface.replace("'", '"'))
            self.assertIn('event.key !== "Enter"', surface.replace("'", '"'))
            self.assertIn("event.shiftKey", surface)
            self.assertIn("event.preventDefault()", surface)

    def test_regular_agent_uses_a_bottom_anchored_chat_layout(self) -> None:
        root = Path(__file__).resolve().parents[3]
        regular = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "regular_agent.html"
        ).read_text(encoding="utf-8")
        history = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "agent_chat_history.js"
        ).read_text(encoding="utf-8")

        self.assertIn("height: 100dvh", regular)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", regular)
        self.assertIn(".chat-history-spacer", regular)
        self.assertIn('class="chat-history-spacer"', regular)
        self.assertIn("clamp(38px, 7.2vw, 69px)", regular)
        self.assertNotIn(
            "This surface exposes curated finite Skills",
            regular,
        )
        self.assertLess(
            regular.index('class="answer-panel"'),
            regular.index('class="bottom-dock"'),
        )
        self.assertIn('class="runtime-statuses"', regular)
        self.assertNotIn('class="status-grid"', regular)
        self.assertLess(
            regular.index('id="activity"'),
            regular.index('id="managerState"'),
        )
        self.assertNotIn('id="clearChatHistory"', regular)
        self.assertGreaterEqual(
            history.count(
                'followIfNearBottom(this.state.status === "RUNNING")'
            ),
            6,
        )

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
        self.assertIn("approval.authorization_arguments", app)
        self.assertIn("perform_relative_effector_motion", app)
        self.assertNotIn(
            "JSON.stringify(approval.request || {}, null, 2)",
            app,
        )
        self.assertIn("approval.title", regular)
        self.assertIn("approval.authorization_arguments", regular)
        self.assertIn("perform_relative_effector_motion", regular)
        self.assertIn("/api/streaming-runs/", regular)

    def test_regular_agent_has_bounded_session_authorization_controls(
        self,
    ) -> None:
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

        self.assertIn('id="autoApproveProviders"', regular)
        self.assertIn('id="autoApproveMoves"', regular)
        self.assertIn('id="autoApproveCalibration"', regular)
        self.assertIn('id="autoApproveProviderStop"', regular)
        self.assertIn('id="autoApproveSafeHome"', regular)
        self.assertIn('id="autoApproveSpaceReinitialization"', regular)
        self.assertIn('id="maxAutoMoveCm"', regular)
        self.assertIn('id="maxAutoSpeedMps"', regular)
        self.assertIn(
            'id="autoApproveProviders" type="checkbox" checked',
            regular,
        )
        self.assertIn(
            'id="autoApproveMoves" type="checkbox" checked',
            regular,
        )
        self.assertIn(
            'id="autoApproveCalibration" type="checkbox" checked',
            regular,
        )
        self.assertIn(
            'id="autoApproveProviderStop" type="checkbox" checked',
            regular,
        )
        self.assertIn(
            'id="autoApproveSafeHome" type="checkbox" checked',
            regular,
        )

    def test_regular_agent_uses_replayable_backend_owned_stream(self) -> None:
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

        self.assertIn('@app.post("/api/streaming-runs"', app)
        self.assertIn('media_type="text/event-stream"', app)
        self.assertIn("AgentRunStreamRegistry", app)
        self.assertIn('new EventSource(started.events_url)', regular)
        self.assertIn('event.type === "assistant.message.delta"', regular)
        self.assertIn('event.type === "skill.retry.recovered"', regular)
        self.assertIn('event.type === "skill.retry.exhausted"', regular)
        self.assertIn('id="agentImageInput"', regular)
        self.assertIn('id="stopRunButton"', regular)
        self.assertIn("started.cancel_url", regular)
        self.assertIn('event.type === "run.cancelled"', regular)
        self.assertIn('@app.post("/api/streaming-runs/{run_id}/cancel"', app)
        self.assertIn('fetch("/api/agent-attachments"', regular)
        self.assertIn("attachment_ids: attachmentIds", regular)
        self.assertIn(
            'event.type === "assistant.reasoning_summary.delta"',
            regular,
        )
        self.assertIn('id="chatHistory"', regular)
        self.assertIn('/assets/agent_chat_history.js', regular)
        self.assertIn("turn.appendReasoning", regular)
        self.assertIn(
            'id="autoApproveCalibrationActivation" '
            'type="checkbox" checked',
            regular,
        )
        self.assertIn('value="120"', regular)
        self.assertIn('id="maxAutoSpeedMps" type="hidden" value="5"', regular)
        self.assertIn('value="5"', regular)
        self.assertIn(
            'midbrain.regularAgent.sessionAuthorization.v4',
            regular,
        )
        self.assertIn('max="120"', regular)
        self.assertIn("auto_authorize_provider_activation", regular)
        self.assertIn("auto_authorize_relative_motion", regular)
        self.assertIn("max_auto_speed_m_s", regular)
        self.assertIn("auto_authorize_stationary_calibration", regular)
        self.assertIn("auto_authorize_stationary_activation", regular)
        self.assertIn("autoApproveCalibrationActivation", regular)
        self.assertIn("AUTO_PROVIDER_ACTIVATION", regular)
        self.assertIn("AUTO_BOUNDED_RELATIVE_MOTION", regular)
        self.assertIn("AUTO_STATIONARY_CALIBRATION", regular)
        self.assertIn("NEW_RELATIVE_POSE_MOVE", regular)
        self.assertIn("NEW_RELATIVE_ROTATION", regular)
        self.assertIn("APPLY_CONTROLLED_FRAME_YAW_DELTA", regular)
        self.assertIn("controlled-frame yaw AUTO <= 45°", regular)
        self.assertIn('["start", "hot", "warm"]', regular)
        self.assertNotIn("execute_integrated_motion_preview", regular)
        self.assertIn("Provider stop AUTO", regular)
        self.assertIn("safe-home AUTO", regular)
        self.assertIn("_validate_automatic_agent_approval", app)
        self.assertIn(
            '"midbrain-autonomous-agent-systemic-gui-v5-"',
            app,
        )
        self.assertIn("agent_runtime_session_epoch = uuid.uuid4().hex", app)
        self.assertIn("SessionSettings(limit=None)", app)
        self.assertIn("session_history_item_limit=", app)

    def test_developer_view_uses_the_shared_autonomous_agent_controls(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'id="autoApproveProviders" type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveProviderStop" type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveMoves" type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveCalibration" type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveCalibrationActivation" '
            'type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveSafeHome" type="checkbox" checked',
            app,
        )
        self.assertIn(
            'id="autoApproveSpaceReinitialization" type="checkbox"',
            app,
        )
        self.assertIn('id="maxAutoMoveCm"', app)
        self.assertIn('value="120"', app)
        self.assertIn('id="maxAutoSpeedMps"', app)
        self.assertIn('value="5"', app)
        self.assertIn(
            "midbrain.developerAgent.sessionAuthorization.v1",
            app,
        )
        self.assertIn("automaticDeveloperApprovalDecision", app)
        self.assertIn("AUTO_PROVIDER_ACTIVATION", app)
        self.assertIn("AUTO_BOUNDED_RELATIVE_MOTION", app)
        self.assertIn("AUTO_STATIONARY_CALIBRATION", app)
        self.assertIn("AUTO_STATIONARY_ACTIVATION", app)
        self.assertIn("AUTO_PROVIDER_STOP", app)
        self.assertIn("AUTO_SAFE_HOME", app)
        self.assertIn("AUTO_SPACE_REINITIALIZATION", app)
        self.assertIn("NEW_RELATIVE_POSE_MOVE", app)
        self.assertIn("NEW_RELATIVE_ROTATION", app)
        self.assertIn("APPLY_CONTROLLED_FRAME_YAW_DELTA", app)
        self.assertIn("controlled-frame yaw AUTO <= 45°", app)
        self.assertIn("auto_authorize_provider_activation", app)
        self.assertIn("auto_authorize_provider_stop", app)
        self.assertIn("auto_authorize_relative_motion", app)
        self.assertIn("max_auto_move_cm", app)
        self.assertIn("max_auto_speed_m_s", app)
        self.assertIn("auto_authorize_stationary_calibration", app)
        self.assertIn("auto_authorize_stationary_activation", app)
        self.assertIn("auto_authorize_safe_home", app)
        self.assertIn("auto_authorize_space_reinitialization", app)
        self.assertIn("Provider stop AUTO", app)
        self.assertIn("safe-home AUTO", app)
        self.assertNotIn('"/api/dev/streaming-runs",', app)
        self.assertNotIn('"/api/dev/streaming-runs/{run_id}/decision"', app)
        self.assertIn("driver = _build_autonomous_agent_driver()", app)
        self.assertNotIn("developer_driver =", app)
        self.assertNotIn("_developer_agent_step", app)
        self.assertIn("fetch('/api/streaming-runs'", app)
        self.assertIn("'/api/streaming-runs/'", app)
        self.assertNotIn("fetch('/api/run'", app)
        self.assertIn("new EventSource(started.events_url)", app)
        self.assertIn("consumeStreamingDeveloperRun", app)
        self.assertIn("assistant.message.delta", app)
        self.assertIn("assistant.reasoning_summary.delta", app)
        self.assertIn("skill.retry.recovered", app)
        self.assertIn("skill.retry.exhausted", app)
        self.assertIn('id="developerAgentImageInput"', app)
        self.assertIn('id="stopRun"', app)
        self.assertIn("started.cancel_url", app)
        self.assertIn("event.type === 'run.cancelled'", app)
        self.assertIn("fetch('/api/agent-attachments'", app)
        self.assertIn("attachment_ids: attachmentIds", app)
        self.assertIn('id="developerChatHistory"', app)
        self.assertIn('/assets/agent_chat_history.js', app)
        self.assertIn("turn.appendReasoning", app)
        self.assertNotIn("runLegacyDeveloper", app)

    def test_agent_surfaces_share_safe_visual_evidence_viewer(self) -> None:
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
        viewer = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "visual_evidence.js"
        ).read_text(encoding="utf-8")
        chat_history = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "agent_chat_history.js"
        ).read_text(encoding="utf-8")
        package_config = (
            root / "test_agent" / "python" / "pyproject.toml"
        ).read_text(encoding="utf-8")

        for surface in (app, regular):
            self.assertIn('/assets/visual_evidence.js', surface)
            self.assertIn('/assets/agent_chat_history.js', surface)
            self.assertIn("visual.evidence.created", surface)

        self.assertIn("createVisualEvidenceElements", chat_history)
        self.assertIn("new window.MidbrainVisualEvidenceViewer", chat_history)
        self.assertIn('copyButton.textContent = "Copy annotated"', chat_history)
        self.assertIn(
            'downloadButton.textContent = "Download PNG"',
            chat_history,
        )

        self.assertIn(
            '@app.get("/api/visual-evidence/{evidence_id}/channels/{channel_id}")',
            app,
        )
        self.assertIn("document.createElementNS", viewer)
        self.assertIn('this.overlay.setAttribute(', viewer)
        self.assertIn('"viewBox"', viewer)
        self.assertIn("navigator.clipboard.write", viewer)
        self.assertIn("downloadAnnotatedImage", viewer)
        self.assertIn("ANNOTATION_PALETTE", viewer)
        self.assertIn("renderColorControls", viewer)
        self.assertIn("this.colorFor(annotation, index)", viewer)
        self.assertIn(
            'ANNOTATION_LABEL_HALO = "rgba(0, 0, 0, 0.72)"',
            viewer,
        )
        self.assertIn("ANNOTATION_LABEL_WEIGHT = 500", viewer)
        self.assertIn("elements = {}", viewer)
        self.assertIn("this.labelFontSize(width)", viewer)
        self.assertIn("this.labelHaloWidth(width)", viewer)
        self.assertNotIn("visualOverlayColor", viewer)
        self.assertNotIn("innerHTML", viewer)
        self.assertIn('"web/*.js"', package_config)

    def test_agent_surfaces_share_bounded_robot_local_session_history(
        self,
    ) -> None:
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
        history = (
            root
            / "test_agent"
            / "python"
            / "physical_agent_test"
            / "web"
            / "agent_chat_history.js"
        ).read_text(encoding="utf-8")

        self.assertIn('id="chatHistory" class="chat-history"', regular)
        self.assertIn('id="developerChatHistory" class="chat-history"', app)
        self.assertNotIn('id="clearChatHistory"', regular)
        self.assertNotIn('id="clearDeveloperChatHistory"', app)
        self.assertIn("DEFAULT_MAXIMUM_TURNS = 40", history)
        self.assertNotIn("window.sessionStorage.setItem", history)
        self.assertNotIn("window.sessionStorage.removeItem", history)
        self.assertNotIn("bindRuntimeEpoch", history)
        self.assertIn("agent_runtime_session_epoch", app)
        self.assertIn('fetch("/api/chat-session"', regular)
        self.assertIn("fetch('/api/chat-session'", app)
        self.assertIn("agentChatHistory.hydrate", regular)
        self.assertIn("developerChatHistory.hydrate", app)
        self.assertIn("setInterval(loadChatSession, 3000)", regular)
        self.assertIn("setInterval(loadChatSession, 3000)", app)
        self.assertIn("this.localOwner = !restoring", history)
        self.assertIn("midbrain.agent_chat_turn.v1", history)
        self.assertIn("updateFromServer(nextState)", history)
        self.assertIn("renderEventsPreservingExpansion", history)
        self.assertIn("serverStateRevision(state)", history)
        self.assertIn("const wasNearBottom = this.nearBottom()", history)
        self.assertNotIn("if (!locallyOwned.includes(turn))", history)
        self.assertIn("In-progress execution summary", history)
        self.assertIn("Model-provided reasoning summary", history)
        self.assertIn("visual_evidence: null", history)
        self.assertNotIn("tool_arguments", history)
        self.assertNotIn("tool_output", history)

    def test_world_point_cloud_shows_live_local_axes_and_keeps_gravity(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('aria-label="World coordinate legend"', app)
        self.assertIn('<strong class="world-x">+X</strong>', app)
        self.assertIn('<strong class="world-y">+Y</strong>', app)
        self.assertIn('<strong class="world-z">+Z</strong>', app)
        self.assertIn('id="axisControls"', app)
        self.assertIn('id="fitAxes"', app)
        self.assertIn('id="frameLabels"', app)
        self.assertIn('id="screenAxisOverlay"', app)
        self.assertIn("refreshSpatialAxes", app)
        self.assertIn("dynamicAxisFrames", app)
        self.assertIn('"camera_pose": camera_frame', app)
        self.assertIn(
            "updateCameraMarker(cameraPose && cameraPose.available ? cameraPose : null)",
            app,
        )
        self.assertNotIn("poseData.world_from_camera", app)
        self.assertIn("ARM_BASE", app)
        self.assertIn("GRIPPER_TOOL", app)
        self.assertIn("ARM_JOINT", app)
        self.assertIn("OBJECT", app)
        self.assertIn("SCREEN_2D", app)
        self.assertIn("Shift-drag", app)
        self.assertIn("↓ World gravity · -Z", app)
        self.assertIn("cloudData.world_frame", app)
        self.assertIn("cloudData.transform_authority", app)
        self.assertIn("cloudData.calibration_revision", app)
        self.assertIn("map transform: ", app)

    def test_spatial_axis_inspector_reuses_point_cloud_renderer(self) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('@app.get("/dev/spatial-axes"', app)
        self.assertIn("return PAGE", app)
        self.assertIn("spatial-inspector-mode", app)
        self.assertIn("Fit visible axes", app)
        self.assertIn("metadata", app)

    def test_ui_shutdown_invokes_workspace_stop_script_directly(self) -> None:
        root = Path(__file__).resolve().parents[3]
        manager_ui = (
            root / "platform_core" / "manager" / "src" / "ui.rs"
        ).read_text(encoding="utf-8")

        self.assertIn('.join("stop_workspace.ps1")', manager_ui)
        self.assertNotIn('.join("stop_workspace_delayed.ps1")', manager_ui)

    def test_runtime_status_exposes_scene_policy_restore_diagnostics(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        app = (
            root / "test_agent" / "python" / "physical_agent_test" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"scene_policy_restore": {', app)
        self.assertIn('"result": scene_policy_restore_result', app)
        self.assertIn('"error": scene_policy_restore_error', app)


if __name__ == "__main__":
    unittest.main()
