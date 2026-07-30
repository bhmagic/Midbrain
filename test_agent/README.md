# Physical Agent Test Scaffold 0.3.0

The browser service has two intentionally separate OpenAI Agents SDK surfaces:

- `http://127.0.0.1:8000/` is the regular Agent UI. It can prompt the
  regular allowlisted Agent and may propose `start`, `hot`, `warm`, or `stop`
  for a required configured Provider. Every lifecycle change pauses for
  operator approval. It also exposes the narrow relative-IK workflow, but not
  the complete developer tool catalog.
- `http://127.0.0.1:8000/dev` is the developer Agent UI. It may inspect every
  adapter-bound discoverable Skill and Provider status, and may propose
  `start`, `hot`, `warm`, or `stop` for an exact configured Provider. Every
  lifecycle tool invocation uses the Agents SDK approval interruption and must
  be explicitly approved before the saved run resumes.

Either Agent may propose `HOT` when a cold Provider is a necessary dependency
of the requested task; neither can execute the transition until the operator
approves the exact tool call. Approval prompts show a human-readable action,
Provider, requested state, and hardware warning rather than raw SDK JSON. When
a visual Skill has no current camera frame, it returns
`PROVIDER_ACTIVATION_REQUIRED` with the relevant Midbrain developer-boundary
URL instead of leaking a raw Fabric HTTP 404.

Both Agent profiles offer an Integrated Controller relative-IK workflow.
Its discoverable Skill contract declares the activation sequence explicitly:
`robot_arm.rebot_dm` must become `HOT` first, followed by
`robot_arm.primary.integrated`. The Agent inspects runtime and proposes those
approval-gated transitions before it requests a preview.
It first stages a nonphysical preview from the current measured controlled
frame. If the preview is valid, it requests execution of the exact preview and
pauses for a separate readable physical-motion approval. Approval immediately
sends the Integrated Controller's existing one-shot commit input; no separate
LB press is required. The adapter then waits at most 15 seconds for a new
terminal trajectory result and reports success only when the controller sets
`completion_success` to true. `UP` means positive Y in the arm-base frame
because gravity is negative Y. A single relative request is limited to 20 cm,
and the execution adapter rejects a changed, expired, or already-used preview.
The operator no longer has to construct a separate decision ID.

Every relative request is a new displacement from the current measured pose,
including a repeated request after an unconfirmed arrival. The Agent does not
expose a separate prior-target retry tool.

Both profiles also expose a separate approval-gated Basic Controller safe-home
tool. It preempts operational control, preserves the measured gripper angle,
uses the controller's configured home policy, and reports completion only from
`last_safe_home_result.success`. Gravity float, healthy status, and Provider
stop are not accepted as substitutes for homing.

Each Agent UI provides independent selectors for the OpenAI Agent model,
reasoning effort, and configured visual backend. The default Agent model is
`gpt-5.6-terra`; selectable defaults are Terra, Sol, and Luna, with reasoning
from `low` through `max`. Visual selection is limited to backends actually
configured by available API keys. `Auto routing` preserves the existing
ordered VLM fallback, while an explicit visual selection uses only that model
for the run.

The developer surface deliberately has broader administrative reach than the
regular Agent. Use the Midbrain observation pages first; their developer link
requires a separate confirmation that the operator is overstepping the
ordinary agent workflow.

When this optional service is started, it runs Initialize Space Cognition
automatically approximately one second after startup unless
`AUTO_INITIALIZE_SPACE_COGNITION=false`. The workspace launcher sets that
variable to `false` for `-StartAgentUi`, so an explicitly opened Agent surface
still begins idle and prompt-driven. The normal workspace launch does not
start this service.

The current OpenAI Agents SDK evaluation is intentionally narrow. The
`identify_pointed_object` function-tool description is the agent-visible Skill
descriptor. `OPENAI_AGENT_TOOL_CHOICE=auto` lets the model stop cleanly after a
terminal tool error instead of repeatedly invoking an unavailable dependency.
`OPENAI_AGENT_MAX_TURNS` defaults to `16`, is bounded to the range `1` through
`32`, and allows multi-approval workflows to finish without removing the
existing run timeout.

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

Run `scripts/setup.ps1`, then `scripts/run.ps1`, or start it together with the
core using
`platform_core\scripts\run_workspace.ps1 -StartAgentUi`.
