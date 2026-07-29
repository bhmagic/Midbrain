# Phase 5 Progress and Validation Evidence

Date: 2026-07-29
Status: active; the narrow real OpenAI Agents SDK no-contact physical
checkpoint and final safe shutdown are complete. Live camera validation,
immutable replay capture, same-frame
generic/direct comparison, spatial enforcement, Manager-owned reviewed
workcell activation, signed controller-owned transit execution, and one
guarded no-contact object-observation transit are validated. The compound
pointed-object Agent Skill remains disabled, so this is a successful guarded
hardware checkpoint rather than the Phase 5 exit. See
`PHASE5_AGENT_SDK_COMPLETION_AND_SHUTDOWN_20260729.md`.

## Current policy state

- capability binding: `SHADOW`;
- controller submission audit: `ENFORCED`;
- Manager authority: `SHADOW`;
- generic RGB-D route: `SHADOW`;
- Agent SDK physical execution:
  `ENABLED ONLY FOR THE DECISION-ID-ONLY REVIEWED EXECUTION SKILL`;
- Phase 5 spatial binding: `ENFORCED`; and
- Phase 5 spatial generic RGB-D route: `ENFORCED`.
- reviewed stationary-workcell activation: `ENFORCED`;
- final reviewed stationary-workcell activation: `REVOKED AFTER USE`;
- signed UI-decision transit commit: `IMPLEMENTED, CONFIGURATION-GATED`; and
- compound pointed-object Agent tool: `DISABLED`; and
- Cartesian natural-language axis interpretation: `OPEN REVIEWED LIMITATION`.

The two Phase 5 spatial switches are independent of the global Phase 4
switches. The signed transit execution endpoint is exposed to the Agent SDK
only through `execute_reviewed_observation_motion`, whose schema contains one
decision ID and no motion-bearing argument. A separate approved decision and
exact one-time assertion remain mandatory.

## 2026-07-29 completion update

- The real OpenAI Agents SDK selected the reviewed execution Skill.
- The exact approved 40-stage transit completed with no contact.
- A later platform/authority-loss event correctly left normal endpoint hold
  and requested the gravity-supported error fallback.
- A final operator-authorized lift was limited by controller safety policy:
  14 cm and greater were rejected, 13 cm previewed valid, and approximately
  12.67 cm was measured before deadline float.
- Authoritative safe-home completed.
- Test Agent, all Providers, Manager, Fabric, and controller services stopped;
  final process and listener checks found no remaining Midbrain service.
- Cartesian-axis understanding/alignment remains an explicit open challenge;
  see `CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md`.

## Completed implementation evidence

- The Phase 4 checkpoint is closed with explicit Phase 5 carryovers.
- Replay bundles copy current shared-memory payloads while BufferRef generations
  are valid, hash and redact their contents, and rematerialize them under a
  separate `REPLAY` named-memory namespace.
- Replay policy is structurally incapable of starting hardware providers,
  acquiring a physical lease, calling a physical controller, or enabling Agent
  physical execution.
- All planned finite Agent SDK adapters are registered behind explicit manifest
  allowlisting:
  - stationary workcell calibration;
  - general VLM scene analysis;
  - RGB-D spatial registration; and
  - review-only tool-to-control-frame registration.
- The stationary calibration adapter is deferred until invocation,
  approval-required, does not claim the arm is home, and does not allow an
  active-control interrupt.
- Spatial registration:
  - binds camera semantic capabilities through Manager first;
  - retains a visible explicit provider fallback in non-enforced modes;
  - copies synchronized RGB-D immediately;
  - revalidates the binding after capture;
  - rejects mixed provider, instance, or boot identities across route, bundle,
    and calibration;
  - applies the Skill's own source-age and association policy to timestamped
    Fabric observations;
  - prefers the generic route and preserves the direct route as a compatibility
    fallback;
  - respects independent RGB and registered-depth grids and the provider's
    valid region;
  - queries the exact camera timestamp and VIO epoch from Fabric; and
  - never submits physical action.
- Generic RGB-D descriptors are now rejected unless they declare:
  - shared-memory-only large payload transport;
  - independent RGB, infrared, native-depth, and registered-depth grids,
    strides, valid regions, timestamp policies, sample formats, and calibration
    references;
  - support for resolution, aspect-ratio, and boundary mismatch;
  - synchronized-bundle policy; and
  - a provider-custom depth-to-RGB alignment whose output boundary agrees with
    the registered-depth channel and whose calibration revision agrees with the
    route product.
- An invalid generic descriptor is excluded with stable issue codes. A direct
  compatibility route remains selectable in non-enforced mode with
  `INVALID_GENERIC_ROUTE_EXPLICIT_PROVIDER_FALLBACK`; spatial generic-route
  enforcement still rejects that downgrade.
- A synthetic non-Orbbec descriptor passes with deliberately different RGB,
  infrared, native-depth, and registered-depth geometry plus a cropped output
  boundary. This validates the producer-neutral semantic contract. It does not
  yet provide a live brand-neutral shared-memory reader: provider-supplied
  transport adapters remain allowlisted, and the current live adapter is the
  Orbbec CameraHost v2 reader.
- Tool registration:
  - obtains three structured VLM landmarks;
  - allows reflective landmarks to request closest-valid depth;
  - revalidates camera and robot-transform bindings after the VLM call;
  - composes tool-to-base under the robot boot epoch with base-to-workcell under
    the VIO epoch;
  - returns a review-only candidate;
  - does not publish or activate a control frame; and
  - never submits physical action.
- General effector-front registration:
  - defines front as most distal along the current rigid effector/tool assembly
    away from the wrist, not closest to the camera or an action point;
  - gives the VLM lossless RGB, registered-depth, and depth-validity panels on
    the native registered-depth grid;
  - requires an exact valid registered-depth pixel and never silently snaps to
    a neighboring surface;
  - falls back from a reflective, sharp, or thin nominal tip only to the most
    distal valid point on the same rigid assembly;
  - returns both fronts for a bare two-jaw gripper and averages the two
    registered 3D points rather than their pixels;
  - revalidates camera binding and source age after the VLM call;
  - remains read-only and publishes neither motion authority nor a control
    frame; and
  - is discoverable and adapter-bound but intentionally absent from the active
    Agent allowlist pending recorded and live nonphysical review.
- Agent tool callbacks now retain their own manifest descriptor and closed JSON
  schema when multiple tools are eligible.

## Replay enforcement evidence

An enforced `spatial.registration.rgbd` test uses a CameraHost-compatible
Windows named-memory replay mapping through the normal shared-memory reader. It
validates generation, route identity, current Manager binding, generic-route
enforcement, exact timestamp, VIO epoch, calibration, and metric registration.
The result records `physical_action_submitted=false`; replay policy records
physical controller calls as unavailable.

The live bundle `phase5-live-rgbd-20260727-2213` validates all payload hashes
and rematerializes RGB, native depth, and registered depth through the normal
shared-memory reader under replay-only mappings. A same-frame replay comparison
at RGB pixel `[900, 1090]` passed:

- generic route:
  `camera.rgbd.shared_memory.flexible.v1`;
- direct compatibility route:
  `camera.femto_bolt.rgbd.windows_shared_memory.v2`;
- registered depth: `0.7289999723 m` for both routes;
- camera point:
  `[0.0898669511, 0.2323075251, 0.7289999723] m` for both routes;
- maximum point delta: `0.0 m`; and
- hardware start, physical lease, controller call, and Agent physical
execution: all unavailable.

The stationary route comparator now derives one common observation directly
from that replay manifest and verifies the actual RGB and registered-depth
bytes before producing the fingerprint. The installed-gripper observation
fingerprint is
`6ee2acfbd6b76e87fb254d8db9f91e44cd7ec1ee05842a62f8dcefccc1f24b3d`.
This proves common capture identity; it is not yet a Provider-versus-local pose
result pair.

The replay capture path was also corrected so it collects optional Fabric
metadata before acquiring the live bundle references, then copies every
referenced shared-memory payload back-to-back before doing any disk writes.
This removes the observed ring-slot recycling race without weakening BufferRef
generation validation.

An explicit Manager-loss replay scenario completed with the expected
`BINDING_OR_AUTHORITY_UNAVAILABLE` rejection and no hardware or controller
access. Regression coverage also verifies that the visible explicit provider
ID remains available in advisory/fallback mode, while enforced generic routing
does not silently downgrade to the direct route.

All sixteen declared replay scenarios now compute observed outcomes from their
injected evidence instead of echoing expected labels. Coverage includes
success, stale RGB/depth, independent frame cadence, flexible channel geometry,
recycled BufferRefs, alignment revision changes, provider restart, VIO epoch
change, stale workcell transform, rejected controller preview, lease loss,
stale fencing generation, audit persistence failure, Manager loss, and Fabric
loss. Every scenario asserts that hardware startup, physical leases, controller
calls, and Agent execution remain unavailable.

New bundles validate payload hashes and size ceilings and carry a manual
retention-review date. Automatic deletion is forbidden; removal requires an
explicit operator action. A read-only browser provenance panel and API report
the manifest digest, capture routes, provider instance/boot identities,
payload hashes and grids, timestamps/generations, calibration revision, VIO
epoch, world frame, recorded-artifact presence, replay isolation, and retention
policy. Older bundles remain readable and are labelled
`UNSPECIFIED_LEGACY_BUNDLE` rather than being assigned invented provenance.

## Fresh preview authorization checkpoint

The Test Agent front/top observation proposal is the first non-cutting caller
selected for controller-owned path planning. It submits target pose and
constraints to Integrated and owns no interpolation, waypoint timing,
singularity escape, collision evaluation, or speed schedule.

Integrated now returns a short-lived versioned transit-preview contract. The
contract binds:

- the normalized request and SHA-256 digest;
- camera provider, instance, and boot;
- Manager binding;
- workcell transform identity, revision, and expiry;
- VIO session epoch and observation lifetime;
- semantic-scene revision;
- controller provider, instance, boot, and configuration digest;
- the current lease/fencing snapshot;
- issue and expiry timestamps; and
- a digest over the complete planning result and unsigned contract.

The Test Agent independently recomputes both request digests and the complete
preview digest. It also requires a valid collision-free selected plan, matching
plan IDs, unchanged control/lease state, a current scene/transform/observation,
and a nonphysical contract that grants no commit authority. Stable structured
issue codes explain every rejection.

Authorization is created only after that validation, references the exact
preview authority, and is capped so it cannot outlive the preview. Approval
records only a decision and cannot execute motion.

The execution boundary is now implemented. An approved decision may mint one
short-lived HMAC assertion bound to the exact controller
provider/instance/boot/configuration, plan, request, preview, semantic scene,
decision, resolver, and expiry. The assertion and stored plan are one-time.
`POST /v1/motion/path-commit` performs a bounded direct Manager check for the
exact active workcell calibration, verifies its camera/VIO identities and
expiry, rechecks the current scene and collision result, rejects excessive
measured start drift, validates the fenced Basic lease and global inhibit, and
executes only the controller's exact waypoint list.

Adaptive subdivision limits adjacent joint steps. Separate whole-transit
endpoint and aggregate-travel limits no longer misuse the operator single-move
limit. Execution advances only after stable measured arrival at each waypoint,
caps all joints at 0.25 rad/s and Cartesian speed at the controller limit, and
holds the final POS_VEL endpoint until explicit release. Errors, lease loss,
inhibit, platform loss, or stage timeout request gravity-float. The exact
request and assertion hash are written synchronously to the provider-local
audit; the raw assertion is never stored and Fabric remains outside the motor
command path.

Preview expiry is clamped so it cannot outlive either the workcell activation
or source observation. Commit also rejects a Manager activation that was
revoked, expired, or identity-changed after preview.

The planned compound `observe_pointed_object_from_pose` manifest is no longer
advertised as runnable in normal Agent discovery. The driver has no bound
adapter for it, and the current pointing Skill returns an explanatory VLM
answer rather than the structured image pixel and uncertainty needed by RGB-D
registration. The manifest remains visible through debug discovery with an
explicit disabled reason. Re-enabling it requires a structured pointing result,
the complete replay-tested nonphysical adapter, and real model-selected tool
evidence; a hard-coded pipeline is not accepted as a substitute.

## Software matrix

The complete stopped/nonphysical software matrix now passes 567 tests:

- Test Agent: 87;
- Integrated Controller: 95;
- Basic Controller: 83;
- Orbbec provider: 10;
- Local VIO: 30;
- FoundationPose compatibility provider: 43;
- platform core: 30 (`22` Manager and `8` Fabric);
- local vegetable-cutting prototype: 107;
- stationary calibration Skill: 68;
- spatial registration Skill: 4; and
- tool registration Skill: 3; and
- effector-front Skill: 7.

All packages were rerun from the current workspace source roots with bounded
commands. The Test Agent run includes the deterministic replay and temporal
policy cases. No service, camera, controller, hardware endpoint, or VLM was
started by this matrix. The formally accepted stopped/nonphysical
cross-package local floor is now `578/578` (548 Python and 30 Rust tests).
The sanitized GitHub candidate passes `469/469` after excluding the
local-only cutting prototype.

## Gate 4 software comparison checkpoint

Stationary calibration Skill version `0.6.0` adds a versioned route-run and
route-comparison contract for `PROVIDER_COMPATIBILITY` and `SKILL_LOCAL`.
Comparison records:

- recompute a SHA-256 fingerprint from exact RGB, registered-depth, camera
  route, calibration, camera boot, VIO epoch, timestamp, and gripper
  configuration provenance;
- refuse to compare different cases or observation fingerprints;
- record route latency, translation/rotation delta, per-route repeatability,
  structured failure clarity, and operator interaction counts;
- require zero owned estimator sessions after each route;
- require confirmed GPU release and a closed `SKILL_LOCAL` backend;
- reject any physical action, controller call, or control-mode change; and
- remain `COMPARISON_EVIDENCE_ONLY` and never motion-usable.

`SKILL_LOCAL` no longer keeps its FoundationPose-compatible backend alive for
the resident browser Skill lifetime. A new backend is created per finite
calibration attempt, every tracking session is reset by the engine, and the
backend is closed before samples can return. A cleanup failure rejects an
otherwise successful route result.

The stationary-calibration Skill suite passes `68/68`, Test Agent passes
`93/93`, the complete stopped local matrix passes `578/578`, and the exact
GitHub candidate passes `469/469`.

This checkpoint does not claim a live route comparison. The historical
Provider captures predate the new common record, and no `SKILL_LOCAL` result has
yet been produced from the same immutable RGB-D observation.

Calibration result schema v3 now embeds an expiring candidate with:

- workcell/calibration revision and estimator route/mode/version;
- confidence and bounded translation/rotation residual evidence;
- camera provider, instance, boot, generic route, calibration revision,
  timestamp, frame number, and source BufferRefs;
- VIO world frame and session epoch;
- candidate world/VIO and world/base transforms; and
- `CANDIDATE_REVIEW_REQUIRED` plus `motion_usable=false`.

Candidate review remains `SHADOW` by default at the producer. Shadow mode keeps
the tagged legacy transform streams for observation, while `ENFORCED` mode
publishes only `.candidate` streams. Fabric now enforces the semantic state
rather than trusting the stream suffix: invalid, expired,
`motion_usable=false`, and `CANDIDATE_REVIEW_REQUIRED` observations stay in raw
stream history but cannot enter the transform graph. The local cutting
consumer also rejects pending or expired alignments before planning.

The browser now lists candidate digest, expiry, camera and VIO provenance, and
decision state. Approve/reject creates a separate append-only idempotent record
only after a decision-scoped signed identity assertion. Approval is
`APPROVED_FOR_ACTIVATION`, remains `NOT_ACTIVATED`, never modifies the
calibration, and keeps `motion_usable=false`. Without the external identity
secret the endpoint fails closed.

Manager-owned activation is now implemented at
`POST /v1/workcell-calibrations/activate`. It verifies the exact candidate and
decision digest, signed reviewer identity, current camera
provider/instance/boot/calibration, current tracking VIO epoch/frame,
confidence/error bounds, frame semantics, and expiry. Only one unexpired
activation may be active. Explicit revocation publishes a newer
non-motion-usable edge, and Fabric suppresses the older static transform.
Live end-to-end activation still requires a fresh camera/VIO calibration run.

## Camera-access diagnosis

The original failures were reproducible only when `CameraHost.exe` inherited
the restricted Codex workspace process token. In that context, Windows Media
Foundation failed at `MFCreateDeviceSource` with `0x80070005` (`Access is
denied`), while DirectShow could still read the camera interfaces.

The following comparison isolates the cause:

- the current and frozen pre-Phase-1 `CameraHost.exe` SHA-256 hashes are
  identical;
- the current and frozen `OrbbecSDK.dll` hashes are identical;
- native source, CMake configuration, provider configuration, Python native
  launch code, and Manager spawn logic are unchanged;
- a direct bounded launch inside the Codex workspace sandbox fails immediately
  at Media Foundation access; and
- the same executable launched outside that restricted sandbox remains healthy
  for the bounded observation period.

Therefore the camera, Orbbec binaries, project configuration, and Windows
camera settings were not the cause. No camera or Windows privacy setting was
changed. The privacy page had been opened during diagnosis, but no setting was
modified. Hardware camera providers that require Media Foundation must be
started through the bounded workspace launcher outside the restricted Codex
process sandbox.

## Live nonphysical RGB-D validation

The bounded non-arm workspace launch outside the restricted process sandbox
started Fabric, Manager, CameraHost, Local VIO, and the Test Agent. Basic and
Integrated remained stopped.

Live numerical and VLM content validation passed:

- RGB: `1920x1080`;
- native depth: `640x576`;
- registered depth: `1920x1080`;
- independent channel cadence: approximately `30 Hz`;
- RGB/registered-depth timestamp skew: approximately `1.1 ms`;
- registered-depth valid fraction: approximately `0.614`;
- RGB/registered-depth boundary intersection-over-union:
  `0.997599`;
- selected route: generic shared-memory route;
- direct Codex multimodal review: `PASS`; the roll, mat, table, and robot-base
  silhouettes are mutually consistent across RGB, registered depth, and
  overlay, with no visible global translation, scaling, or aspect-ratio error;
  and
- the invalid side regions in the registered-depth rendering agree with the
  provider-declared valid boundary.

Two Gemini API validations were previously used during recovery and
post-restart confirmation because the instruction to use VLM was
misinterpreted. They remain recorded as historical provenance, but are not the
basis for the visual acceptance above. Future operator-requested visual checks
use Codex's own multimodal inspection unless an external model/API is
explicitly requested. One pre-VLM capture attempt rejected an expired
BufferRef and did not call a model. Live spatial registration then passed with
both spatial binding and spatial generic-route enforcement enabled, current
provider instance/boot identity, current VIO epoch, and no physical action.

After validation, the bounded workspace was stopped. No workspace listener
remained on ports 7001, 7002, 7101, 7102, 7103, 8000, 8791, or 8793, and no
`CameraHost` process remained.

The synchronized composite was reviewed again on 2026-07-28 with Codex's
built-in multimodal input. The manual evidence record is
`test_agent/screenshots/rgbd_qc_builtin_review_20260728T174727Z.json`, bound to
image SHA-256
`cffba48ffbf3e1d21e196e6294bb0fc340f37ed86ed71ca0de9786e0d7839202`.
It confirms visible same-scene content and alignment across the table, robot,
refrigerator, chairs, floor, valid-depth boundary, and toilet-paper roll. This
is replay/QC evidence only and cannot authorize motion from stale coordinates.

The current live retry remains blocked before capture. Manager lifecycle was
cleared from stale HOT state and the camera was started from COLD twice without
changing settings. Both fresh CameraHost attempts failed at
`MFCreateDeviceSource` with `0x80070005` followed by the Orbbec control-transfer
error. Windows currently reports two Zoom processes and many Chrome processes;
the camera control port remains unreachable. No unrelated process was
terminated. Until camera ownership is released, fresh RGB-D inspection, VIO,
workcell activation, object registration, and physical interaction remain
fail-closed.

## Guarded 3 cm controller regression

The user authorized one bounded physical regression from safe-home: an upward
Cartesian target of at most `0.03 m`, followed by safe-home. Free workspace
motion, gripper commands, and mode/lease experiments were not authorized.

The bounded core started without the Test Agent UI. Basic started on `COM3` in
`SAFE_HOLD_GRAVITY_FLOAT`; all seven active modes were `IMPEDANCE`, hardware
I/O and mode-switch failure counts were zero, and no operational command was
pending. Initial safe-home reported `already at safe-home` and preserved the
measured gripper angle at `-0.3625926971435547 rad`. Integrated then started
HOT with one fenced Basic lease, `PRESS_MIT`, `ONE_SHOT`, and
`POSITION_3DOF`.

A malformed first nonphysical staging request was rejected because the
PowerShell payload serialized four target elements. No commit, Basic command
submission, gripper command, or physical motion occurred. The corrected
request was decoded and checked before submission as exactly three finite
values. Its immutable physical boundary was:

- measured origin:
  `[0.2585326071, -0.0045310159, 0.2109939822] m`;
- staged target:
  `[0.2585326071, -0.0045310159, 0.2409939822] m`; and
- exact requested delta: `[0, 0, 0.03] m`.

Plan `2674b237-4fd5-496e-be1f-7f7535845645` was collision-free, unclamped, and
valid, with no physical execution blockers, `0.0000422306 m` predicted
position residual, and `0.0512342` minimum continuity sigma. Planning remained
`SHADOW_NONPHYSICAL`; physical authority was added only by the separate
Engage-plus-LB one-shot.

Exactly one commit ran for `3.0 s`. Integrated sent 150 frames, reported one
skipped scheduling frame, performed no replan, and returned to confirmed
gravity-float. The measured controlled frame ended at
`[0.2589543655, -0.0045335892, 0.2331549320] m`, a `0.022161 m` upward change
from the recorded origin and therefore within the authorized `0.03 m`
ceiling. The terminal record correctly remained
`DEADLINE_FLOAT_BEFORE_ARRIVAL`, with `0.0077575 m` deadline Cartesian
residual; the command was not retried or misreported as successful arrival.

Post-motion checks found:

- Integrated and Basic health `HEALTHY`;
- Basic state `SAFE_HOLD_GRAVITY_FLOAT`;
- all seven modes `IMPEDANCE`;
- no active mode transition;
- one commit and no duplicate submission;
- zero hardware I/O and mode-switch failures;
- zero gripper commands and unchanged gripper angle; and
- strict local audit persistence for planning, Engage, commit, and LB release.

Integrated entered WARM and released its lease before final safe-home. Basic
safe-home attempt 2 completed successfully, preserved the gripper at
`-0.3625926971435547 rad`, and returned to healthy gravity-float. The
Manager-owned ordered shutdown completed in `7.3 s`; afterward there were no
listeners on ports 7001, 7002, 8791, or 8793 and no workspace PID registry.

## Prior acceptance gates (now superseded)

1. Release the external Media Foundation camera owner, then start the camera
   once and inspect a fresh synchronized RGB-D composite with built-in
   multimodal review.
2. Start Local VIO, create a fresh stationary-workcell candidate, perform the
   explicit review, activate it through Manager, and test explicit revocation
   before creating the final activation.
3. Run the actual model-selected pointed-object loop against the current live
   observation; do not reuse replay pixels or stale world coordinates.
4. Stage the exact semantic scene, create a controller-owned preview, inspect
   the requested Cartesian/joint speed limits and all collision/singularity
   evidence, then create a decision-specific authorization.
5. Execute one conservative top-observation transit only after all gates pass,
   verify the final endpoint hold and idle behavior, perform only the permitted
   soft-object gripper interaction, and explicitly release or safe-home.
6. Exercise activation revocation, assertion replay, lease loss, inhibit, and
   explicit release without expanding the physical scene.
7. Run `PROVIDER_COMPATIBILITY` and `SKILL_LOCAL` from one immutable recorded
   observation and validate the new comparison record.
8. Rerun the complete stopped multi-package regression matrix and publish the
   new accepted floor.

## Guarded live object-observation checkpoint

The fresh live run completed the perception-to-controller hardware checkpoint
without object contact. It does not claim that the disabled compound Agent SDK
tool selected or executed the sequence.

Fresh built-in multimodal inspection accepted the exact calibration and
execution RGB frames. No external VLM API was called during this checkpoint.
The execution frames showed a clear robot corridor; a person seen in an
earlier calibration frame was behind the work area and had left the execution
frames.

Candidate `20260728T231931Z-a8c16fd2` was rejected because its one-sided
gripper localization produced an `83.99 mm` shift. The rejection exposed a
lineage bug: enforced `vlm_gripper_only` refinement used the newest local file
even when that candidate had been rejected. Enforced mode now selects only the
newest exact-digest Manager-activated lineage whose activation is `ACTIVE` or
`EXPIRED`; it excludes rejected, revoked, unreviewed, and digest-mismatched
records. The complete stationary-alignment suite passes `68/68`.

Clean candidate `20260728T233158Z-3f5c4673`:

- parent: `20260728T215112Z-68614e22`, the last Manager-verified prior;
- translation refinement: `8.14474 mm`;
- rotation refinement: `0 rad`;
- confidence: `0.82`;
- candidate digest:
  `a1a6080e8945a52a70009ffc309223c71b617e692a9b6e392fba4023870ab050`;
- signed review decision:
  `d63e9bf0-3b64-4c55-a159-5b3090230df6`; and
- activation:
  `bc199a1c-2109-489a-a2b4-7f123417a1f5`, later explicitly revoked.

The successful controller-owned plan was
`e6d2dda6-d820-4700-88bf-f228bcd3f911`. It used a fresh binding, a semantic
scene with nineteen table `KEEP_OUT` primitives and one toilet-roll
`WORK_OBJECT`, and an exact one-time signed authorization. Planning selected
the direct collision-free strategy with:

- requested and effective Cartesian speed: `0.05 m/s`;
- controller joint-speed cap: `0.25 rad/s`;
- minimum predicted clearance: `0.1530259 m`;
- minimum Jacobian sigma: `0.0608862`;
- forty measured-arrival stages; and
- final joint-position error below `0.0008 rad`.

The first commit attempt rejected a stale semantic scene before motion. The
same immutable scene revision was restaged and revalidated immediately before
the accepted commit. The accepted commit appears once in the provider-local
strict audit, held the final endpoint, and required explicit release.

The gripper opened and closed in free space while the arm remained inside the
same controller-owned final-hold envelope. No second writer, lease change,
float transition, fault, or object contact occurred. Built-in multimodal
inspection showed symmetric opening and no obvious visible damage. Measured
gripper feedback briefly reached approximately `0.330 rad/s` while opening
and `0.256 rad/s` while closing even though the target-rate command was
`0.2 rad/s`. Because that exceeded the user's `0.25 rad/s` all-joint ceiling,
the run stopped before lowering into contact. Contact is not part of the first
Gate 10 observation move and remains unauthorized until the measured-speed
behavior is corrected and revalidated.

The final code audit also found that gripper `STOP` cleared only the UI request
and left the final-hold action latched. `STOP` now clears the active action and
target; the next controller-owned envelope omits joint 7, causing Basic to
recapture the measured gripper angle under local impedance support while the
six arm joints remain held. Eight focused controller/gripper/lease tests pass.

Basic now treats the gripper FORCE_POS request's
`velocity_limit_rad_s` as a physical ceiling. It translates that ceiling to
the hardware-native velocity field with an explicit scale and applies a
measured-feedback hysteretic brake/hold when reported speed reaches the
requested ceiling. Telemetry exposes the requested and native limits, resume
threshold, current and peak measured speed, hold position, trip count, and
last trip. The Basic suite passes `83/83`, including trip, hold, and resume
coverage. The scale is a conservative software correction derived from the
observed mismatch; it has not been physically calibrated or revalidated and
therefore does not reopen contact authorization.

After explicit path release, Integrated entered `WARM` and released Basic
lease generation 27. Basic safe-home attempt 3 succeeded at the configured
`0.25 rad/s` ceiling, preserved the measured gripper angle
`-0.3625927 rad`, and settled in healthy gravity-float with maximum measured
velocity `0.007326 rad/s`.

Manager shutdown execution
`928ae0c5-0396-4d3f-9770-1d213f0cd509` stopped Integrated and fenced new
authority, then correctly entered `BLOCKED_SAFETY_SUPPORT_RETAINED` because
Basic had been started outside the current Manager process. Basic's local
authoritative `safe_home_then_stop` endpoint completed the required fallback.
Camera, VIO, and the alignment UI were then stopped. Ports `7101`, `7102`,
`7103`, `8011`, `8791`, and `8793` were closed while Manager and Fabric
temporarily remained available for audit inspection. Before the final stopped
regression, Manager and Fabric were also stopped. The current listener audit
finds ports `7001`, `7002`, `7101`, `7102`, `7103`, `8000`, `8011`, `8791`,
and `8793` closed, with no matching arm, camera, VIO, FoundationPose, or Test
Agent process.

## Enforced binding fallback checkpoint

The explicit provider-ID fallback lifecycle now has stopped-state enforcement
coverage. An enforcing consumer rejects a cold fallback binding with
`FALLBACK_REQUIRES_ACTIVATION`; binding does not implicitly start a provider.
When that same explicit provider is independently HOT, ready, healthy, and
advertising the capability, Manager returns it as a `CURRENT`
`AVAILABLE_CAPABILITY` selection. The consumer records the configured fallback
provider ID and validates the exact selected instance and boot. This preserves
deterministic recovery without allowing enforced binding to silently use a
cold or unverified route.

## Authority coordination shadow checkpoint

Integrated now publishes versioned
`physical_agent.authority_coordination_state` evaluations comparing Manager
task authority, Integrated operational-writer activity, and the authoritative
Basic residency lease. Manager and Basic fencing generations are identified as
separate namespaces and are never numerically compared. Holding the Basic lease
while HOT and idle is classified as standby, not as an authority conflict. An
active writer requires exact upstream owner and Manager authority lease IDs to
prove lineage. The evaluator reports stable states and disagreement reasons,
and counts polls, transitions, states, and disagreements.

The evaluator remains `SHADOW_OBSERVE` and cannot replace the local Basic
lease, switch mode, or submit a motor command. Integrated does not yet receive
the upstream Manager owner and authority lease ID at the active action
boundary, so a Manager-authorized active writer correctly reports
`DUAL_LAYER_UNCORRELATED` with `AUTHORITY_LINEAGE_NOT_BOUND`; HOT idle reports
standby without that false disagreement. Binding the three-layer lineage,
expanding the full failure matrix, and enforcing one reversible non-motion
transition remain open.

`PHASE5_AUTHORITY_LINEAGE_DECISION.md` records the steady route: bind Manager
task authority into a versioned Integrated active-action context and retain the
Basic lease across ordinary task changes. Reacquiring the hardware lease per
task is rejected because it would couple upstream scheduling to hardware
support and mode transitions.

## Current Phase 5 decision

Phase 5 is not complete. The guarded hardware checkpoint passed, but the
formal exit still requires:

1. recorded and live nonphysical validation of the new general
   effector-front landmark plus a real OpenAI Agent SDK model-selected
   selected-Skill loop; specialized action-point landmarks and the compound
   pointed-object tool remain deliberately on hold;
2. live matching `PROVIDER_COMPATIBILITY` and `SKILL_LOCAL` calibration
   results, followed by a FoundationPose retirement decision;
3. completion of the versioned Manager/Integrated/Basic authority and failure
   matrix;
4. the shared browser-only neutral dark theme and decision surfaces;
5. physical calibration and validation of the gripper measured-speed guard
   before any future contact work; and
6. final API, package-manifest, operator-documentation, and release-evidence
   reconciliation after the remaining enforcement work.

## Passive Fabric and Skill temporal-policy decision

The Fabric remains passive and optimized to return timestamped frames and
metadata quickly. It does not own one universal definition of stale: the same
observation may be unsuitable for motion control, acceptable for object
registration, and permanently useful for replay.

Each Skill owns its maximum source age, association tolerance, required
provider/boot/revision/epoch, allowed transform extrapolation, post-compute
continuity check, and recovery decision. A slow VLM or neural inference records
the original source timestamp separately from inference start and completion;
publishing the result does not make its source observation new.

Fabric still reports hard structural invalidity such as recycled BufferRefs,
obsolete producer boots, invalid observations, authority revocation, and
producer-declared hard expiry. Timestamp-nearest queries associate channels but
do not promise that the bundle remains fresh after Skill computation.

The first general structured landmark is now resolved:
`locate-effector-front` reports exact registered-depth-grid evidence for the
most distal valid point on the rigid effector/tool assembly, or both distal
points of a bare two-jaw gripper. Specialized action-point landmarks and the
compound pointed-object pipeline remain paused; no placeholder action geometry
or hidden complete VLM pipeline will be introduced.
