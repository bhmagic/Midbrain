# Physical Agent Test Scaffold 0.3.0

The test agent runs Initialize Space Cognition automatically approximately one second after the GUI service starts unless `AUTO_INITIALIZE_SPACE_COGNITION=false`.

The current OpenAI Agents SDK evaluation is intentionally narrow. The
`identify_pointed_object` function-tool description is the agent-visible Skill
descriptor, and `OPENAI_AGENT_TOOL_CHOICE=required` requires the model to select
an offered Skill during the initial test. Set it to `auto` after multi-Skill
routing evaluations are ready.

Before reading the current camera frame, the pointing Skill requests an
advisory `camera.rgb` binding from the Manager and supplies
`HEAD_CAMERA_PROVIDER_ID` only as a fallback. If the new Manager endpoint is
unavailable, the existing explicit Orbbec path continues and the fallback is
reported in the Skill result.

`GET /api/skills` exposes discoverable manifest metadata without importing or
starting Skill implementations. Add `?include_disabled=true` to include local
manual-only or incomplete Skills such as the preserved vegetable-cutting
prototype and the planned compound pointed-object observation Skill. The latter
is not offered to the Agent until structured pointing-pixel output and its
complete replay-tested nonphysical adapter exist.

`POST /api/phase5/spatial/register` is a bounded read-only spatial-registration
route. It binds the current camera instance through Manager, immediately copies
the synchronized RGB-D payload, validates route/bundle/calibration identity,
applies its own source-time and association limits, queries the exact-timestamp
Fabric transform, and never submits a physical action. Fabric receipt time does
not make an old source frame fresh. Replay supplies an explicit historical-data
policy while preserving the original source timestamps. The corresponding
Agent SDK tool remains off the allowlist until replay and live image-alignment
gates pass.

Under enforced binding, a cold explicit provider fallback is rejected rather
than implicitly started. Once that same fallback is independently HOT, ready,
healthy, and advertising the capability, it may be selected as a current
binding; the configured fallback ID and exact selected provider instance/boot
remain in the adapter provenance.

`POST /api/observation-motion/propose` remains nonphysical. It builds a
front/top observation target, calls Integrated's
`POST /v1/motion/path-plan` directly with a bounded timeout, and creates a
decision-specific authorization only when Integrated returns a current
`SHADOW_NONPHYSICAL` contract with a valid collision-free selected plan. The
request must include `preview_context` containing the Manager binding, camera
provider/instance/boot, workcell transform identity/revision/expiry, VIO epoch,
observation timestamp/expiry, and semantic-scene revision. The Test Agent
recomputes the exact request, context, and complete-preview SHA-256 digests and
binds authorization to the controller provider/instance/boot/configuration,
plan, scene, lease snapshot, and preview expiry. A rejected, incomplete,
tampered, restarted, or expired preview creates no authorization. The decision
expires no later than the preview, and approval never executes the proposal.

After an explicit approval,
`POST /api/observation-motion/execute/{decision_id}` is the distinct action
that atomically mints one short-lived HMAC assertion and passes the exact
digests and assertion to Integrated `POST /v1/motion/path-commit`. For a
separate external caller,
`POST /api/authorizations/{decision_id}/execution-assertion` returns the
one-time assertion instead; that caller then submits the direct Integrated
commit and must not call the Test Agent execution route. The authorization
store retains the assertion ID and SHA-256, not the token. A second mint,
replay, changed plan, changed controller identity, expired preview, or
non-approved decision fails closed. Execution completion keeps the controller
endpoint active; the caller must use Integrated
`POST /v1/motion/path-release` when gravity-float is intended.

`GET /api/phase5/replay/{bundle_id}/provenance` is read-only. The corresponding
browser panel shows bundle and manifest identity, routes, payload hashes and
geometry, provider identities, timestamps/generations, calibration/VIO/world
provenance, record-presence gaps, replay isolation, and manual retention
review. It cannot start hardware or call a controller.

The browser UI exposes independent indicators for:

- Visual correction state and selected source (`RGBD`, `IR_DEPTH`, or no accepted update).
- Inertial pose propagation mode and IMU integration step count.
- Rotation estimator source.
- Gravity adjustment (`OFF`, `READY`, or `ACTIVE`).
- Feature extraction mode and low-light candidate selection.
- Map capture state.
- Initialization/reset state and session epoch.

The UI distinguishes pose propagation from visual correction. A pose may continue updating from IMU samples while the visual correction light is stale or unavailable. Visual correction diagnostics include accepted/rejected state, selected RGB-D or IR/depth source, reprojection error, correction magnitude, and time since the latest accepted visual update.

The gravity lamp retains the established behavior. Gravity changes roll and pitch only, preserves yaw and translation, and uses startup gyroscope bias/noise measurements to determine the effective quiet threshold.

The world RGB point cloud remains an orthographic isometric view. Orange marks world down and cyan shows the current camera frustum. Points are transformed into the current VIO world frame and fade over ten seconds.

Run `scripts/setup.ps1`, then `scripts/run.ps1`.
