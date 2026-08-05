# Physical Agent Test Scaffold 0.4.4

The browser service exposes two views of one autonomous OpenAI Agents SDK
runtime:

- `http://127.0.0.1:8000/` is the regular Agent UI. It can prompt the
  configured autonomous Agent and may call `start`, `hot`, `warm`, or `stop`
  for a required configured Provider. The browser-session policy can authorize
  exact lifecycle transitions without repeated questions. It also exposes the
  configured relative-IK workflow.
  The regular UI starts a backend-owned streamed run and observes it through a
  replayable SSE connection; closing that connection does not cancel or restart
  the run. The canonical `/api/streaming-runs` family is the only Agent
  execution contract.
- `http://127.0.0.1:8000/dev` is a developer view of that same Agent. It adds
  Provider, Skill, replay, point-cloud, and exact normalized-event diagnostics,
  but its prompt uses the same driver, model session, tool policy, Provider
  lifecycle behavior, authorization policy, retry behavior, and backend-owned
  streaming path as the regular page. It submits directly to the canonical
  `/api/streaming-runs` family and has no developer-specific execution API.
- `http://127.0.0.1:8000/dev/run-journal` is the read-only durable run viewer.
  Its left pane groups familiar run cards under expandable Manager-boot
  sessions; its right pane presents the selected outcome and two-level
  expandable normalized event detail. It cannot
  resume a run, decide an approval, delete a record, or invoke a robot command.

When `HOT` is necessary for the requested task, the Agent calls the lifecycle
tool directly instead of answering with a conversational permission request.
The tool's dynamic approval predicate is the authorization boundary. An
eligible browser-session policy is evaluated before execution and therefore
does not create an SDK interruption or a separate resume request. Otherwise,
approval prompts show a
human-readable action, Provider, requested state, and hardware warning rather
than raw SDK JSON. When a visual Skill has no current camera frame, it returns
`PROVIDER_ACTIVATION_REQUIRED` with the relevant Midbrain developer-boundary
URL instead of leaking a raw Fabric HTTP 404.

For a finite-Skill dependency, `HOT` is used even when the Provider process is
stopped because Manager includes startup in that transition. Accepting the
control request is not treated as completion. The lifecycle tool polls fresh
Manager evidence for up to
`PROVIDER_HOT_READINESS_TIMEOUT_S` (45 seconds by default) and completes only
after the Provider is `HOT` and ready. A Skill-provided `required_capability`
such as `camera.rgb` is also checked when present. After readiness, the Agent
must immediately invoke the original finite Skill in the same run. The visual
adapter retains a separate `CAMERA_FIRST_FRAME_TIMEOUT_S` data-plane check so a
late or recycled first frame does not reopen the lifecycle transition. A
transient timeout retries only that capture boundary, twice total by default,
using `CAMERA_SKILL_CAPTURE_ATTEMPTS` and
`CAMERA_SKILL_RETRY_BACKOFF_S`. The binding is retained, inference runs only
after capture succeeds, and the result explicitly records fresh-evidence and
no-physical-action retry provenance.

If the model chooses `START` with a non-null required capability, the same
readiness gate applies; it does not return immediately at process creation. A
plain `START` with a null capability remains process-only. A timed-out
capability-bearing `START` returns the exact `HOT` tool continuation, preventing
an immediate finite-Skill retry against a degraded startup heartbeat.

On approval resume, the host passes the saved `RunState` back without replacing
its SDK context. This retains the exact approved/rejected call record and the
browser-session authorization, so the protected tool runs once and the Agent
continues its original task. The host also fingerprints protected operations by
tool name and canonical arguments, excluding transient SDK call IDs. A
genuinely new duplicate request is stopped rather than shown or executed
again.

Both the regular and developer views have browser-session switches for Provider
activation and stop, physical relative-pose motion, stationary world-to-arm
calibration and exact candidate activation, Basic safe-home, and spatial
reinitialization. A fresh browser session defaults Provider activation and
stop, bounded motion, calibration, exact candidate activation, and safe-home
on. Spatial
reinitialization remains off because it deliberately changes the world epoch
and invalidates dependent state. Relative motion has
operator-entered distance and nominal-speed maxima and can automatically
approve only `execute_integrated_motion_preview` for the exact staged preview
at or below both limits. The motion authorization also covers a bounded
controlled-frame yaw of at most 45 degrees; that hard Skill limit is not
operator-expandable. Calibration authorization accepts only
`calibrate_stationary_workcell`. The server revalidates the exact tool and
motion shape, distance, planned nominal speed, and yaw as applicable before the
SDK decides whether to pause; controller and Provider-side safety checks remain
active.
The authorization control accepts the full 120 cm free-space request envelope.
Actual reach is still decided by controller IK, joint ranges, and semantic
scene clearance; CONTACT_WORK retains a separate 20 cm short-stroke policy.
An operation outside the active session policy produces a normal SDK approval
interruption for development. The policy decision never disables Provider or
controller checks.

The shared Agent offers an Integrated Controller relative-IK workflow through
either browser view.
Its discoverable Skill contract declares the activation sequence explicitly:
`robot_arm.rebot_dm` with `robot.motion.arm.basic` must become `HOT` first,
followed by `robot_arm.primary.integrated` with
`robot.motion.arm.integrated.mit.one_shot`. The Agent inspects runtime and
requests those authorization-gated transitions by directly calling the
lifecycle tool before it requests a preview. If the model supplies a capability
name that the selected Provider does not advertise, the lifecycle boundary
does not wait for that impossible name. It waits for Provider readiness and
continues to the finite Skill, whose adapter validates the exact operation
capability.
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
`ARM_BASE_POSITIVE_*` names. A single free-space relative request accepts up to 1.2 m,
and the execution adapter rejects an expired or already-used approval. A
periodic semantic-scene update may invalidate the controller's internal
preview without changing the approved target; in that case the adapter
re-previews that exact target once against the newest scene after rechecking
the measured start and spatial frame. Pre-commit failures do not consume the
approval. The operator no longer has to construct a separate decision ID.

Move-close item requests use the composed no-contact workflow rather than a
model-invented relative direction. Each iteration locates the item and gripper
front in parallel, requests a controller-owned collision-checked path preview,
and exposes a separate `execute_no_contact_approach_step` tool containing only
the exact preview identifiers and bounded step metadata. Host authorization
then mints a short-lived controller assertion and executes that one preview.
The default correction cap spans the complete 1.2 m arm ROI and requests a
0.12 m/s nominal endpoint speed, so the first accepted path reaches the
uncertainty-expanded destination. The five-centimeter diagnostic cap is no
longer a default. A caller can still request a smaller explicit cap.
The no-contact runtime binds `WAIT_FOR_NEXT`, so measured arrival keeps the
endpoint under at least 1x impedance plus Basic gravity feed-forward while the
next parallel observation and signed correction are prepared. A compatible
next commit chains without an intermediate float or mode transition; the
bounded wait expires to verified gravity float when no continuation arrives.
The execution result explicitly marks `WAITING_NEXT`, `HOLDING_FINAL`, and
verified `COMPLETED_FLOAT` as `measured_arrival_confirmed=true`. The Agent must
therefore run the returned post-move observation instead of describing
`WAITING_NEXT` as an unconfirmed move.

RGB-D input follows Fabric best-available snapshot semantics. Bundle freshness
is evaluated when the exact Fabric-referenced payload is copied, not after
Manager binding checks, VLM inference, scene compilation, or Agent reasoning.
Those later delays remain timestamped provenance and are governed by the
item/effector Skill's task-age policy. The controller independently revalidates
the newest semantic scene and measured start state at preview/commit. A Skill
must not poll a camera Provider control API to make downstream computation look
fresh.
The loop stops at the uncertainty-expanded standoff, a controller/perception
rejection, or the configured iteration limit. Planning alone never counts as
movement, and every step retains the no-contact policy even when the target is
a touchable workpiece.

Obstacle mapping accepts only explicit user/upstream object descriptions; it
never defaults a black mat or table to `KEEP_OUT`. Each explicit request starts
a new mapping epoch. The HOT tracker runs VLM annotation, SAM2 object and arm
segmentation, and a second VLM mask-quality review before sphere fusion. It
repeats that complete chain at most three times and publishes an explicit
zero-sphere failure after the third rejection. The scene inspector waits for
the compiler to name the exact requested policy revision, then opens
switchable RGB, registered-depth, and reviewed-mask evidence. The developer 3D
viewer remains the sphere visualization, with independent legend/toggles and a
reduced sample; Integrated consumes the full scene.

An optional `requested_speed_m_s` is interpreted as nominal average endpoint
speed. The adapter requests `distance_m / requested_speed_m_s` seconds from the
existing one-shot controller, within its 0.05-to-60-second representable
window. There is no independent Cartesian speed ceiling: the preview converts
the request to per-joint demand, requires explicit authentication above 10
rad/s on any joint, and rejects a request at or above 20 rad/s. Provider and
motor POS_SPEED caps may lengthen execution. The result reports requested,
planned, and controller-reported duration and never describes the
joint-interpolated motion as constant instantaneous Cartesian velocity. The
legacy browser speed field remains accepted for request compatibility but is
not an authorization gate; the per-joint 10/20 rad/s policy is authoritative.

Every relative request is a new displacement from the current measured pose,
including a repeated request after an unconfirmed arrival. The Agent does not
expose a separate prior-target retry tool.

The shared Agent also exposes a Basic Controller safe-home tool. It preempts
operational control, preserves the measured gripper angle, uses the
controller's configured home policy, and reports completion only from
`last_safe_home_result.success`. The shared session policy may authorize this
exact recovery operation without a dialog. Gravity float, healthy status, and
Provider stop are not accepted as substitutes for homing.

Each Agent UI provides independent selectors for the OpenAI Agent model,
reasoning effort, and configured visual backend. The default Agent model is
`gpt-5.6-terra`; selectable defaults are Terra, Sol, and Luna, with reasoning
from `low` through `max`. Visual selection is limited to backends actually
configured by available API keys. `Auto routing` preserves the existing
ordered VLM fallback, while an explicit visual selection uses only that model
for the run.

Both Agent prompt panels accept one optional JPEG, PNG, or WebP image up to
8 MiB. The browser previews it, uploads it to bounded Midbrain process memory,
and places only the returned attachment ID in the regular or developer run
request. The OpenAI adapter resolves that ID into text-plus-image Agent input.
This image is user context for the selected intellectual Agent model; it is not
a Fabric observation and cannot replace the live robot-camera frame used by
Robotics-ER Skills. The local Agents SDK session may retain the multimodal turn
in `agent_sessions.sqlite3` for conversational continuity.

Read-only VLM inference has a bounded transient retry before ordered fallback.
By default, each selected backend may be attempted twice with a 0.25-second
backoff. Only timeouts, transport failures, rate limits, and recognized
transient server status codes are retried; invalid configuration and other
non-transient failures move directly to the next configured backend or fail.
`PHASE4_VLM_ATTEMPTS_PER_BACKEND` is restricted to `1..3`, and
`PHASE4_VLM_RETRY_BACKOFF_S` to `0..5`. The result provenance reports every
failed attempt and whether it was retryable. This does not retry an Agent run
or any physical action.

The developer view exposes broader read-only diagnostics than the regular
view, but it has no broader Agent tool or execution policy. Component-specific
development pages may still require a separate confirmation because their
manual administrative controls can overstep the Agent workflow.

When this optional service is started, it runs the formal Initialize /
Re-establish Space Cognition Skill automatically in initialize-if-needed mode
approximately one second after startup unless
`AUTO_INITIALIZE_SPACE_COGNITION=false`. The workspace launcher sets that
variable to `false` for `-StartAgentUi`, so an explicitly opened Agent surface
still begins idle and prompt-driven. The normal workspace launch does not
start this service.

The shared Agent can call the policy-gated `reinitialize_space_cognition`
tool when a new origin or accepted drift recovery is required. It revokes
active workcell calibration before resetting VIO, clears point observations
from the old epoch, and resumes capture only against the new epoch. It is not a
Provider-readiness probe, and both views leave its automatic authorization off
by default.

The streamed Agent contract is defined in
`contracts/15_agent_event_stream.md`. The browser receives normalized Midbrain
run, message, reasoning-summary, tool, and approval lifecycle events rather
than raw SDK objects or tool arguments. Live SSE replay remains in memory. The
same normalized events are also batched into the bounded robot-local SQLite
journal defined in `contracts/19_agent_run_journal.md`; no browser subscriber
is required. The journal survives process restarts and marks unfinished
prior-process runs interrupted, but it is not yet an authenticated or encrypted
field-audit system.

Both Agent views project those events into a scrollable conversation. Each run
keeps its user prompt, Agent answer, visual-evidence viewer, and an expandable
execution summary. OpenAI runs request the model's public automatic reasoning
summary; safe tool, approval, and retry lifecycle labels remain useful when a
runtime does not emit summary text. Raw chain-of-thought, tool arguments, and
tool outputs are not placed in browser history.

The developer view uses a 50/50 workspace. Independently scrolling,
individually collapsible diagnostics are on the left; its bottom-follow
conversation and prompt composer are on the right. Developer turns add a
two-level event view: the outer disclosure contains the normalized event log,
and each retained event expands to its exact safe envelope. The durable journal
remains the source for history across browser or process restarts.

The regular page uses a fixed chat layout: its compact title remains at the
top, the conversation grows upward inside the only main scroll region, and the
prompt composer remains at the bottom. Compact Manager, Fabric, and Skill
status appears with the activity on the left side of the conversation header.
Active streamed turns follow their newest line. Model selectors are below the
prompt box on both Agent pages. Enter submits the prompt; Shift+Enter inserts a
newline.

The Agent service projects at most the latest 40 runs from the current Manager
boot into both chat surfaces. The prompt, public answer, safe event details,
and model metadata come from the robot-local SQLite journal; attached image
bytes do not. Both pages poll the same `/api/chat-session` projection, so they
show the same transcript when open together and restore it after a tab closes.
The tab that starts a live run retains ownership of that SSE stream while the
other page observes journaled progress. There is no clear-history control.
This robot-local projection is not yet an authenticated field-audit view; see
`contracts/18_agent_chat_history.md` and `contracts/19_agent_run_journal.md`.

When the pointing-identification or general visual-scene Skill analyzes a
camera frame, both Agent pages receive a `visual.evidence.created` event for
the exact retained RGB bytes used by the VLM. The browser renders normalized
point and box records as an SVG overlay, can hide or recolor the overlay, and
assigns distinct deterministic colors with an editable swatch for each
annotation. The same per-annotation colors are used for client-side copy and
download of a flattened PNG. Annotation labels use a compact medium-weight
font with a translucent black halo so the bright annotation color remains
legible without a pale border. The evidence contract can
carry multiple named channels, but the current visual Skills publish only RGB;
they do not attach the separately sampled latest depth image and imply false
RGB-D synchronization. A later synchronized RGB-D producer can add a depth
channel without changing the browser event or renderer.

RGB capture waits up to 12 seconds for the first readable frame after Provider
activation. Missing observations and recycled startup BufferRefs are retried at
the capture boundary without re-running the Agent or VLM. A timeout returns
`CAMERA_FRAME_UNAVAILABLE` with `retry_scope=CAPTURE_RGB_ONLY`; it does not
claim that another Provider activation or physical action occurred.

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

The shared Agent also exposes `calibrate_stationary_workcell`. VIO establishes
and tracks the local world epoch, while this finite Skill observes the
stationary robot base/end effector and creates an immutable world-to-arm-base
candidate. The Agent must then call
`review_and_activate_stationary_calibration` with the exact alignment ID and
candidate digest. Manager independently revalidates quality, provenance,
current camera/VIO identity, calibration revision, VIO epoch/convention, and
tracking health before publishing the transform as motion-usable. A mounted-rig
activation has no wall-clock expiry; it is suspended when tracking evidence is
insufficient and invalidated on identity or epoch change, explicit revocation,
or supersession. Candidate creation and exact activation have separate
browser-session authorization switches. Calibration
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

The single autonomous Agent uses one process-boot-scoped v5 session ID for
requests from either browser view.
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
