# Physical Agent Test Scaffold 0.4.2

The browser service has two intentionally separate OpenAI Agents SDK surfaces:

- `http://127.0.0.1:8000/` is the regular Agent UI. It can prompt the
  regular allowlisted Agent and may call `start`, `hot`, `warm`, or `stop`
  for a required configured Provider. By default every lifecycle change pauses
  for operator approval. The browser-session control below the prompt can
  authorize start/HOT/WARM transitions without repeated questions; stop still
  asks. It also exposes the narrow relative-IK workflow, but not the complete
  developer tool catalog.
- `http://127.0.0.1:8000/dev` is the developer Agent UI. It may inspect every
  adapter-bound discoverable Skill and Provider status, and may propose
  `start`, `hot`, `warm`, or `stop` for an exact configured Provider. Every
  lifecycle tool invocation uses the Agents SDK approval interruption and must
  be explicitly approved before the saved run resumes.

When `HOT` is necessary for the requested task, the Agent calls the lifecycle
tool directly instead of answering with a conversational permission request.
The tool's dynamic approval predicate is the authorization boundary. An
eligible regular-UI session policy is evaluated before execution and therefore
does not create an SDK interruption or a separate resume request. Otherwise,
approval prompts show a
human-readable action, Provider, requested state, and hardware warning rather
than raw SDK JSON. When a visual Skill has no current camera frame, it returns
`PROVIDER_ACTIVATION_REQUIRED` with the relevant Midbrain developer-boundary
URL instead of leaking a raw Fabric HTTP 404.

On approval resume, the host passes the saved `RunState` back without replacing
its SDK context. This retains the exact approved/rejected call record and the
browser-session authorization, so the protected tool runs once and the Agent
continues its original task. The host also fingerprints protected operations by
tool name and canonical arguments, excluding transient SDK call IDs. A
genuinely new duplicate request is stopped rather than shown or executed
again.

Both the regular and developer UIs have separate browser-session switches for
Provider activation, physical relative-pose motion, stationary world-to-arm
calibration, and exact candidate activation. A fresh browser session defaults
all four switches on, with a 35 cm translation authorization ceiling and a
0.5 m/s nominal-speed authorization ceiling. Relative motion has
operator-entered distance and nominal-speed maxima and can automatically
approve only `execute_integrated_motion_preview` for the exact staged preview
at or below both limits. The motion authorization also covers a bounded
controlled-frame yaw of at most 45 degrees; that hard Skill limit is not
operator-expandable. Calibration authorization accepts only
`calibrate_stationary_workcell`. The server revalidates the exact tool and
motion shape, distance, planned nominal speed, and yaw as applicable before the
SDK decides whether to pause; controller and Provider-side safety checks remain
active.
The authorization control accepts up to 100 cm so it can cover future bounded
tools, but the
current relative-motion tool independently retains its 20 cm execution limit.
Safe-home, Provider stop, and other protected operations remain outside these
session authorizations and still ask.

Both Agent profiles offer an Integrated Controller relative-IK workflow.
Its discoverable Skill contract declares the activation sequence explicitly:
`robot_arm.rebot_dm` must become `HOT` first, followed by
`robot_arm.primary.integrated`. The Agent inspects runtime and requests those
authorization-gated transitions by directly calling the lifecycle tool before it
requests a preview.
It first stages a nonphysical preview from the current measured controlled
frame. If the preview is valid, it requests execution of the exact preview and
uses either bounded session authorization or a separate readable
physical-motion approval. Authorization immediately sends the Integrated
Controller's existing one-shot commit input; no separate LB press is required.
The adapter then waits at most 15 seconds for a new
terminal trajectory result and reports success only when the controller sets
`completion_success` to true. Ordinary `UP` means world positive Z, opposite
gravity. The adapter resolves the world vector through the current timestamped
world-to-arm transform before IK. Explicit `world +X/-X/+Y/-Y/+Z/-Z` requests
always require that reviewed motion-usable transform and can never use the
upright arm-mount fallback. Raw arm axes require explicit
`ARM_BASE_POSITIVE_*` names. A single relative request is limited to 20 cm,
and the execution adapter rejects a changed, expired, or already-used preview.
The operator no longer has to construct a separate decision ID.

An optional `requested_speed_m_s` is interpreted as nominal average endpoint
speed. The adapter requests `distance_m / requested_speed_m_s` seconds from the
existing one-shot controller, within its 0.25-to-8-second range. Provider
joint-rate safety may lengthen that duration. The result reports requested,
planned, and controller-reported duration and never describes the
joint-interpolated motion as constant instantaneous Cartesian velocity. The
0.5 m/s browser value is an authorization ceiling for future bounded tools;
the current relative-pose Skill retains its independent 0.2 m/s request limit.

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

When this optional service is started, it runs the formal Initialize /
Re-establish Space Cognition Skill automatically in initialize-if-needed mode
approximately one second after startup unless
`AUTO_INITIALIZE_SPACE_COGNITION=false`. The workspace launcher sets that
variable to `false` for `-StartAgentUi`, so an explicitly opened Agent surface
still begins idle and prompt-driven. The normal workspace launch does not
start this service.

Both Agent surfaces can call the approval-gated
`reinitialize_space_cognition` tool when the operator explicitly requests a new
origin. It revokes active workcell calibration before resetting VIO, clears
point observations from the old epoch, and resumes capture only against the
new epoch. It is not a Provider-readiness probe.

Relative arm motion separates direction authority from visual evidence. A
current reviewed world-to-arm transform is the first authority for all WORLD
directions, including ordinary up/down/front/back/left/right. Only when that
measured resolution is unavailable may the operator confirm an upright arm
base (`+Z` opposite gravity and `+X` toward robot front) as a preview-scoped
fallback. The fallback does not authorize explicit signed world axes. When
measured alignment or optional visual
verification is requested, the operator separately confirms that the camera
and IMU form a rigidly fixed rig. The Agent can then non-destructively start or
verify camera and VIO tracking using a short-lived VIO-local stationary
attestation while preserving the existing epoch. This readiness path never
acquires the global motion inhibit, revokes the Integrated Controller lease,
or calls the destructive reinitialization tool.

Relative translation defaults to position-only 3-DoF IK. When the operator
asks to keep the effector head, pointing direction, attitude, or 3D
orientation, the preview instead copies the current measured controlled-frame
RPY into a `POSE_6DOF` target. Preview approval is bound to that orientation
and is rejected if the measured or staged orientation changes before
execution.

The same Skill supports a bounded controlled-frame head-yaw delta for pure
rotation or a simultaneous translation and rotation. Controlled-frame +X is
forward, +Y is left, and +Z is up, so positive yaw turns left and negative yaw
turns right. The adapter composes the delta as a rotation matrix in the
controlled frame and converts the result to controller RPY; it never adds
Euler components. Pure rotation uses zero translation without inventing a
spatial direction. Both forms use `POSE_6DOF` and the controller's existing
`PRESS_MIT` one-shot preview/execution path. Positional before/after evidence
does not claim to verify the commanded yaw angle.

When fixed-rig tracking and valid exact depth are available, relative-motion
preview persists a gravity-aligned RGB/depth evidence image and effector
landmark before motion. After approved controller execution, it persists a
second image and landmark in the same VIO world/epoch. The result reports
controller completion and the before/after visual displacement verdict
separately. Missing exact depth, an unavailable image, or an unconfirmed fixed
rig makes visual verification skipped or unavailable; it does not block an IK
preview whose direction is already established by a reviewed transform or the
upright-mount fallback. A changed-epoch or ambiguous after observation is
`INCONCLUSIVE`; it is not rewritten as visual success.

The regular Agent also exposes `calibrate_stationary_workcell`. VIO establishes
and tracks the local world epoch, while this finite Skill observes the
stationary robot base/end effector and creates an immutable world-to-arm-base
candidate. The Agent must then call
`review_and_activate_stationary_calibration` with the exact alignment ID and
candidate digest. Manager independently revalidates quality, provenance,
current VIO tracking, and expiry before publishing the transform as
motion-usable for at most five minutes. Candidate creation and exact bounded
activation have separate browser-session authorization switches. Calibration
acquires the global motion inhibit; consequently, Integrated requires an
explicit approved HOT recovery before a later motion preview. Basic safe-home
has the same expected recovery boundary because it preempts Integrated's
Basic-controller lease.

When the current TCP/gripper is too close to the base Z axis to observe yaw,
calibration returns `CALIBRATION_POSE_REQUIRED` with the measured horizontal
lever arms. The Agent should ask the operator to move the effector sideways and
retry; the result is nonphysical and does not claim or activate a transform.

The world RGB point-cloud view renders a labeled world-origin triad using the
same coordinates as the accumulated VIO points: +X front, +Y left, and +Z up.
It also displays the active VIO world-frame identifier and retains the separate
orange -Z gravity/down arrow.

This calibration tool has a dedicated
`STATIONARY_CALIBRATION_TIMEOUT_S` budget, defaulting to 600 seconds, because
FoundationPose model initialization and inference can be GPU-bound. The
calibration adapter latches that longer deadline only after the finite
calibration tool starts; unrelated Agent runs keep the ordinary 90-second hard
deadline.

The current OpenAI Agents SDK evaluation is intentionally narrow. The
`identify_pointed_object` function-tool description is the agent-visible Skill
descriptor. `OPENAI_AGENT_TOOL_CHOICE=auto` lets the model stop cleanly after a
terminal tool error instead of repeatedly invoking an unavailable dependency.
`OPENAI_AGENT_MAX_TURNS` defaults to `16`, is bounded to the range `1` through
`32`, and allows multi-approval workflows to finish without removing the
existing run timeout.

Regular and developer Agent sessions use process-boot-scoped v4 session IDs.
Model input targets the latest `OPENAI_AGENT_SESSION_HISTORY_ITEMS` history
items, defaulting to 32, and expands backward to the beginning of the enclosing
user turn when needed. This prevents Responses reasoning, function-call, and
function-output items from being split at the item-count boundary. Previous
SQLite session histories remain fully stored for audit, but a restarted Test
Agent does not replay an incomplete approval or `required_next_tool` chain from
an earlier process boot. Runtime inspection preserves
the complete current Manager
evidence, including Provider reports, controller telemetry,
command/target/planning/trajectory state, launch commands and arguments,
identities, capabilities, timestamps, and non-secret environment values. Only
duplicate Skill schemas are omitted; credential-like environment values remain
visible by name and are replaced with `[REDACTED]`.

Before reading the current camera frame, the pointing Skill requests an
advisory `camera.rgb` binding from the Manager and supplies
`HEAD_CAMERA_PROVIDER_ID` only as a fallback. If the new Manager endpoint is
unavailable, the existing explicit Orbbec path continues and the fallback is
reported in the Skill result.

`GET /api/skills` exposes discoverable manifest metadata without importing or
starting Skill implementations. Add `?include_disabled=true` to include active
manual-only or incomplete Skills such as the planned compound pointed-object
observation Skill. The archived vegetable-cutting prototype is outside
`skills/` and is never scanned. The planned observation Skill is not offered to
the Agent until structured pointing-pixel output and its complete replay-tested
nonphysical adapter exist.

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

The world RGB point cloud is an orthographic +Z-up view with live local-frame
overlays. World, arm-base, gripper/tool, camera, all six arm links, and any
object-owned transform frames appear in a checkbox list. World, base,
gripper/tool, and camera axes are visible by default; dense joint/object axes
are opt-in. Each line folds open to current transform metadata. Red, green,
and blue are local +X, +Y, and +Z. Orange marks gravity/world -Z, cyan shows
the current camera frustum, and RGB-D samples fade over ten seconds.

`/dev/spatial-axes` reuses this point-cloud renderer as a focused spatial
inspector instead of maintaining a second rotation widget. It adds a larger
canvas, per-frame visibility, frame labels, fit-to-visible-axes, orbit, pan,
and zoom. Explicit 2D screen space is an optional overlay with image +X right,
image +Y down, and a top-left pixel origin. Missing transform paths remain in
the list as unavailable rather than being replaced by guessed axes.

Run `scripts/setup.ps1`, then `scripts/run.ps1`, or start it together with the
core using
`platform_core\scripts\run_workspace.ps1 -StartAgentUi`.
