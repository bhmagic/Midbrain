# Midbrain Reference Agent

This package is the reference adapter between an autonomous Agent runtime and
Midbrain's framework-neutral Skills, events, evidence, lifecycle, and
authorization boundaries. It demonstrates one implementation; it is not the
definition of a Midbrain Agent.

## Surfaces

- `http://127.0.0.1:8000/` redirects to the Developer Agent.
- `http://127.0.0.1:8000/dev` provides the autonomous Agent controls together
  with read-only Provider, Skill, point-cloud, replay, and event diagnostics.
- `http://127.0.0.1:8000/dev/run-journal` provides a read-only view of retained
  normalized run events.
- `http://127.0.0.1:8000/dev/skills/slicing` provides a numeric, non-language
  two-stage Slicing test surface. It freezes an Integrated alignment preview
  and a begin/vector/length-derived three-point path before exposing separate
  physical Stage 1 and Contact Stage 2 controls. It also manages numbered
  blade-use and load/retract/timing profiles without invoking a language Agent.

The Developer Agent uses one backend-owned run path, tool policy, model
session, chat projection, approval store, and event stream. Its diagnostic
panes do not add tools, authority, or an approval bypass. The separately labeled
Slicing test surface does not invoke a language Agent; it calls only the
Skill-owned staged adapter, which retains Manager lifecycle, exact Integrated
signed-preview execution, workcell binding, explicit Integrated `WARM` lease
relinquishment, Contact signing, and terminal relax boundaries.

The Developer Agent exposes **Stop task** for one active backend run. It cancels
that run and its owned asynchronous subtasks without stopping background
Providers. It cannot retract an already accepted controller command or prove a
physical outcome.

## Agent boundary

The Agent may interpret objectives, inspect available capabilities, select
eligible finite Skills, request Provider lifecycle changes through host policy,
evaluate structured results, and decide whether to continue or recover.

The Agent does not receive unrestricted camera, motor, lease, or Provider
control APIs. Physical execution remains behind:

- Manager lifecycle and authority policy;
- current Provider identity and per-capability readiness;
- coherent Fabric evidence;
- controller-owned nonphysical preview;
- exact policy or development authorization;
- Provider-local fencing and command validation; and
- bounded completion and post-action evidence.

An Agent SDK approval interruption is a development interaction mechanism, not
the hardware safety boundary and not a permanent requirement for every field
action.

Routine runtime inspection returns a regulated catalog containing every
Provider and advertised capability without arbitrary heartbeat diagnostics or
launch environment. The top-level Agent can explicitly inspect one complete
sanitized Provider record when necessary; that read is not a lifecycle action
and is unavailable as a Limited Graph child.

Every installed Skill publishes a complete result schema and a smaller compact
pointer tier. Direct and graph calls validate the complete result but return
only compact values plus an opaque detail reference. The top-level Agent may
explicitly read one selected field or the complete sanitized result associated
with that exact reference. Detailed-result storage and inspection are
diagnostic only and cannot authorize, repeat, or change a physical outcome.

For routes that publish a new explicit scene policy, the host combines that
policy publication with the fresh regulated Manager catalog observation in one
FunctionTool result. Provider selection and `set_provider_residency` remain a
separate next call, with unchanged Manager lifecycle, readiness and
authorization ownership. Lifecycle-enabled Agents expose this composed policy
tool instead of also loading the redundant standalone policy schema.

The Agent-facing `run_limited_graph` schema is a concise authoring projection:
ordered Skill steps imply ordinary success edges, while bindings, overrides,
retries, switches and model routes remain explicit. The host deterministically
compiles it into the canonical immutable Limited Graph version 1 shape before
the existing schema validation, static preflight, digest, authorization and
execution paths. Canonical graph callers remain supported at the host callback
boundary. JSON-bearing projection fields retain explicit names:
`value_json`, `args_json`, and `expected_json`. A pre-execution authoring or
static-preflight rejection may be corrected exactly once; it cannot repeat a
started graph or physical action.

The current Limited Graph implementation is accepted as near stable for the
retained linear scene-map, transit, two-slice, intermediate-motion, Provider-
handover, incremental-visual, and separate safe-home workflow. This does not
qualify every branch, retry, switch, model route, multi-visual presentation,
or material-cut outcome. See
[Limited Graph Status and Qualification](../docs/14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md).

Safe Home is a controller-owned recovery operation and never pauses for an
Agent SDK permission decision. The Basic Controller's safety, fencing,
readiness, and controller-confirmed completion checks remain in force.

## Skill discovery and execution

Discovery reads concise manifest metadata without importing or starting Skill
implementations. After selection, the host binds the required adapter and
loads the detailed input schema.

Non-GPT model transports use a client-executed `tool_search` compatibility
function. Concatenated copies of one identical JSON object are collapsed as a
bounded transport recovery. Invalid or ambiguous JSON and unknown Skill names
return a retryable typed search result instead of terminating the Agent run.
The event stream and journal expose only the failure code and bounded numeric
diagnostics; they never expose the raw model arguments or loaded definitions.

When the Developer Agent opens, an installed discoverable Skill outside both
the configured and current runtime Agent lists triggers a startup decision.
**Add to Agent list** records Agent-owned eligibility and requires an Agent
restart before that newly added Skill can run; **Disable for Agent** records an
immediate local exclusion. An unresolved or restart-pending Skill remains
unavailable without blocking tasks that use the already active tool surface.
Decisions are stored in ignored machine-local configuration, never in the
Skill manifest. This keeps installation, Agent eligibility, Skill execution,
and Provider lifecycle as separate responsibilities.

### Routing compatibility

The Reference Agent does not treat work-object, corner, obstacle, or arbitrary
object-category wording as a mutually exclusive action class. Those labels may
identify scene evidence or select a typed coordinate derivation, but they do
not authorize contact and do not hide otherwise eligible motion, grip,
slicing, release, or graph tools. A prompt that combines several operations
keeps the complete eligible surface instead of being routed from its first
verb or one noun.

Narrow deterministic routes remain only for operations whose requested finite
boundary is unambiguous, such as an explicit scene-only inspection, a specific
no-contact approach, or mixed-frame direction translation. Contact Skills do
not consume the semantic collider set; Integrated continues to own free-space
collision checks. Manager and the active assembly resolve Provider/resource
identity, so future multi-arm support does not depend on the model inventing a
physical arm selection from language.

An `EXTERNAL_SKILL_ENTRYPOINT` manifest may also declare a Skill-owned host
adapter factory and setup entrypoint. The Reference Agent loads that factory
through a generic service bundle; Skill-specific RGB-D, FK, VLM, profile, and
state logic remains inside the Skill package. The numerical entrypoint runs in
the Skill's private environment. Manifest latency class selects a bounded
adapter deadline, allowing multi-sample visual work without putting a
Skill-specific timeout in the Agent.

The built-in `move_effector_to_world_point` adapter is a narrow absolute-world
free-space operation. It copies point coordinates and optional spatial-session
identity from the Agent call, resolves them through current Fabric transforms,
preserves measured controlled-effector orientation, and keeps the preview ID
and canonical signed commit envelope out of model-visible arguments. It does
not calculate a relative move in the language model and does not authorize
contact work.

`derive_fabric_world_point` is the read-only coordinate boundary used before
that motion Skill when a target comes from a semantic work-object AABB. It
selects the named corner from the current Fabric observation, converts the
declared offset unit, resolves source/world/controlled-effector axes with
timestamped transforms, and emits the exact three world-target fields consumed
by `move_effector_to_world_point`. The language model selects semantics and
forwards the result; it does not perform vector arithmetic or coordinate-frame
conversion. Derivation freezes one fresh coherent scene snapshot at Skill
invocation. A newer monotonic scene publication does not retroactively
invalidate that decision snapshot, while source expiry and a change of active
world-frame authority still fail closed.

`translate_fabric_direction_to_world` and
`translate_fabric_pose_to_world` are the general read-only coordinate
translators. Direction translation rotates and normalizes a vector without
applying translation. Pose translation applies the complete rigid transform
to a metric position and XYZW quaternion. Both accept active-world, configured
arm-base, or current controlled-effector source coordinates and return the
active world-frame, epoch, calibration, timestamp, and transform path. Their
outputs are evidence for a tandem call; neither tool moves the robot or grants
physical authority.

Mixed-frame slicing uses the direction translator before the unchanged
`slice_with_blade` contract. The Agent copies `direction_world` into the field
with the same semantic role, so an arm-base slicing direction becomes
`slicing_direction_world` while a world blade direction remains
`blade_direction_world`. The pose translator is available for coordinate
composition, but no physical world-pose consumer is implied by its presence.

The developer 3D view draws fresh `WORK_OBJECT` visible-surface AABBs as
wireframe boxes. A work-object label is attached once to its box; individual
dense semantic spheres remain visible but unlabeled. Obstacles receive neither
AABBs nor text labels. The viewer projection does not change the controller's
collision scene or authorize motion.

World-direction and absolute workcell-world resolution give priority to the
active reviewed transform. A
Exact `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2` and
`MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V3` activations remain usable when
Local VIO is temporarily `DEGRADED`, consistent with Manager's invalidation
policy; the current Locate Arm Base path emits V3, while V2 remains compatible
for existing records. Unknown future versions fail closed. The activation is
re-read before commit. The upright arm-mount question is only a bounded
development fallback when no reviewed motion-usable transform exists.

Provider dependencies are made `HOT` through Manager. The host waits for a
fresh Manager report showing the required capability ready; process creation
alone is not success. Visual Skills separately wait for a readable current
camera BufferRef because control-plane readiness does not guarantee that the
first data-plane frame is already usable.

Retries are bounded at named read-only boundaries such as transient visual
inference or initial camera capture. A complete task or physical action is not
automatically repeated.

## Arm-base localization

The built-in `locate_arm_base` adapter delegates the finite visual workflow to
the Skill and delegates activation to Manager. For the final bounded rotation,
the Skill uses exactly one coarse VLM observation of a landmark defined by the
active mounted-effector profile, then compares it with Basic's timestamped FK.
One recognized point is enough; VLM confidence is audit-only and the Agent does
not select the 0/90/180/270-degree candidate.

If that VLM request cannot identify the effector, the adapter returns a
non-terminal retry instruction requiring
`rough_arm_base_positive_x_world`. The Agent must obtain or ask the user for a
rough world-frame vector pointing along arm-base +X and invoke the Skill once
with that argument. The explicit retry performs no effector VLM call. The Agent
must not invent the direction or silently repeat the failed observation.

## Arm-root translation refinement

The discoverable `refine_arm_root_translation` tool performs a non-moving
XYZ-only refinement of an existing motion-usable world-to-arm-base alignment.
Natural requests may specify an adoption factor from zero to one and one to
five samples; both default to one. For example: `Refine the arm alignment with
VLM using 5 samples and adoption factor 0.25.`

The Agent does not choose pixels, repair depth, perform kinematics, average
samples, judge delta limits, or submit the calibration record itself. Those
decisions remain inside the finite Skill and its effector profile. The Agent
may perform at most the typed dependency continuation returned by the Skill:
one HOT recovery followed by one fresh retry. A repeated arm-FK/Fabric timing
failure is reported without a calibration update or recovery loop.

The result exposes exact Midbrain visual evidence for RGB, registered depth,
overlap, selected landmark points, derived 3D midpoint, and old/proposed
alignment projections. A successful result distinguishes the raw estimated
correction from the adopted correction and confirms that rotation was
unchanged and no physical motion was submitted.

## Runs and events

The sole execution family is `/api/streaming-runs` with status, event-stream,
and decision routes. A backend run continues independently of one browser SSE
connection. Reconnection can replay retained in-memory events without
restarting the run.

Agent-runtime-specific events are projected into the
[Agent Event Stream Contract](../contracts/15_agent_event_stream.md). The
browser receives public messages, public reasoning summaries when available,
sanitized tool and lifecycle events, approval state, retry outcomes, and visual
evidence references. It does not receive private reasoning, credentials, or
unrestricted raw tool payloads.

## Chat and journal

One conversation session is associated with the current Manager boot. Opening
another Developer Agent tab or closing a tab does not create a separate
physical authority or erase the robot-local transcript.

The normalized SQLite run journal survives process restarts and marks
nonresumable prior-process runs interrupted. It is diagnostic observation
state only. It cannot execute, cancel, resume, steer, approve, or authorize a
robot action and is not yet an authenticated field-audit store.

See [Chat History](../contracts/18_agent_chat_history.md) and
[Run Journal](../contracts/19_agent_run_journal.md).

## Images and visual evidence

One validated user image may be attached to an Agent turn through an opaque
Midbrain attachment ID. It is conversational input and has no robot-camera
timestamp, depth, calibration, spatial frame, or physical authority.

Visual Skills independently capture current robot-camera evidence. They may
return the exact analyzed channel plus normalized point, box, or vector
annotations. The browser provides per-layer visibility and color controls and
renders overlays without changing the retained source image.

See [Agent Image Attachments](../contracts/17_agent_image_attachments.md) and
[Visual Evidence](../contracts/16_visual_evidence_and_annotations.md).

## Setup and run

From the repository root:

```powershell
.\test_agent\scripts\setup.ps1
.\test_agent\scripts\run.ps1
```

Normal workspace startup uses:

```powershell
.\platform_core\scripts\run_workspace.ps1 -StartAgentUi
```

The desktop `Start Midbrain.cmd` entry point starts the idle Agent service and
links both views from the Manager portal.

Keys, optional model selections, tool eligibility, limits, retry policy,
journal retention, and endpoint settings belong in ignored local
configuration. Do not document active secret values or make a model name part
of the Midbrain compatibility contract.

The Reference Agent model selector is multi-provider while retaining the
legacy `OPENAI_AGENT_MODEL`, `OPENAI_AGENT_MODELS`, and
`OPENAI_AGENT_REASONING_EFFORT` configuration names. A Gemini model ID is
resolved through Google's OpenAI-compatible endpoint and requires
`GEMINI_API_KEY`; a GPT model uses the native OpenAI Agents SDK route and
requires `OPENAI_API_KEY`. Both browser views obtain the allowed reasoning
levels for the selected model from `/api/status` rather than assuming every
provider supports the same set.

The separate **Visual model** selector defaults to Gemini Robotics-ER 2.0 when
that configured backend is available. Its run-scoped selection governs the
shared VLM router and is also passed explicitly to finite Skills such as
`locate_arm_base` that own structured visual judgments internally. This keeps
one operator-visible model choice without moving VLM semantics out of the
Skill's private environment or changing Provider responsibilities.

Both choices still execute through `Runner.run_streamed` and the sole
`/api/streaming-runs` family. Every `gpt-*` model retains the original deferred
Skill tools and native hosted `ToolSearchTool`. Every non-`gpt-*` model receives
the client-executed compatibility path: an ordinary `tool_search` FunctionTool
shows the exact deferred names and descriptions, returns the selected original
full definitions in the completed client envelope, and makes them callable on
the following model turn. Limited Graph stays immediate. This introduces no
new catalog, routing rule, synchronous route, Skill duty, or authorization
boundary. A Chat Completions backend does not acquire native Responses item
types, hosted same-response continuation, or Responses cache behavior.

## Validation

Use the package environment:

```powershell
.\test_agent\.venv\Scripts\python.exe -m pytest -q test_agent\python\tests
```

The suite covers discovery, adapter binding, lifecycle readiness, event
projection, streaming runs, decision handling, visual evidence, attachments,
chat projection, journal behavior, spatial tools, preview integrity, and
nonphysical failure paths. Consult [VALIDATION.md](VALIDATION.md) for the
current scope.

## Current limitations

- The HTTP and browser surfaces are loopback development interfaces without
  field-ready authentication and roles.
- Local chat, journal, attachment, and evidence stores are not encrypted or
  tamper-evident.
- Agent framework and model selection remain reference implementations.
- Physical capability depends on the installed Providers, current evidence,
  configured eligible Skills, authority, and controller qualification.
- Browser disconnect or model cancellation does not prove a physical action's
  outcome.
- Limited Graph's retained linear workflow is near stable, but purpose-built
  live routing-branch coverage, simultaneous multi-visual presentation,
  material-cut sensing, and strict projected-session size bounds remain open.

Outside agent integrations should start with the
[Compatibility and Extension Guide](../docs/05_COMPATIBILITY_AND_EXTENSION.md)
and preserve the same framework-neutral boundaries.
