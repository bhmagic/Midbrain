# Changelog

## Unreleased

- Record the next Agent objectives in the roadmap and version history. The
  immediate performance objective is a measured deterministic command chain
  for frequent robot operations that reduces extra model turns without
  bypassing finite Skills, lifecycle policy, controller checks, leases,
  evidence, or authorization. Contextual development approval cards are marked
  near future; remote command security and durable evidence policy remain
  explicit future work.
- Reconcile periodically synchronized Agent turns in place instead of
  destroying and recreating their DOM. Live local runs retain the correct
  chronological position, unchanged server projections are skipped, expanded
  execution/event disclosures remain open, and bottom-following occurs only
  when the operator was already near the bottom.
- Treat a model-supplied capability name that is absent from a Provider's
  advertised Manager catalog as advisory rather than waiting for an impossible
  match. A healthy `HOT` Provider can continue to its finite Skill, whose
  adapter validates the exact operation capability. Relative arm activation
  instructions and recovery continuations now carry the exact Basic and
  Integrated one-shot capability names.
- Persist the SDK-neutral Agent event sequence in a bounded robot-local SQLite
  journal with Manager-boot session parents, in-place v1 migration, batched WAL
  writes, terminal-run and per-run event retention, restart-safe `INTERRUPTED`
  status for nonresumable runs, and read-only health reporting. Storage failure
  degrades observability without blocking the Agent pipeline. Live SSE, SDK
  sessions, and physical authority remain separate boundaries.
- Store the bounded public Agent transcript under the active Manager boot and
  project it through `/api/chat-session`. The regular and developer pages now
  restore and periodically synchronize the same chat after tab close/reopen,
  while a tab that starts a live SSE run retains local stream ownership. Remove
  the browser clear-history control and browser `sessionStorage` transcript.
- Add a read-only Agent run-journal GUI with expandable Midbrain-session
  parents and familiar run cards on the left, plus a chat-like selected-run
  outcome and category-then-event expansion on the right. Link it from the
  Manager portal and both Agent views without adding command, resume, approval,
  or deletion authority.
- Route the regular page and developer view through one autonomous Agent
  driver, process-scoped model session, tool policy, pending-approval store,
  and streaming/resume implementation. Keep only the canonical
  `/api/streaming-runs` execution family; remove the synchronous `/api/run`
  route and developer execution aliases so diagnostic presentation cannot
  drift into a second behavior policy.
- Reshape the developer view into an equal two-pane workspace with
  independently scrolling, individually collapsible diagnostics on the left
  and a regular-style bottom-follow conversation and prompt composer on the
  right. Developer chat turns can expand twice to reveal each normalized event
  envelope retained in the browser session.
- Reshape the regular Agent page into a fixed-height chat surface with a
  persistent compact header, bottom-anchored scrolling conversation, and a
  bottom composer. Compact Manager, Fabric, and Skill status is grouped into
  the left side of the conversation header. Model selection now sits below the
  prompt; Enter submits while Shift+Enter inserts a newline on both Agent
  prompt surfaces, and active streamed turns follow their newest line.
- Normalize Gemini Robotics-ER `0..1000` annotation coordinates at the VLM
  boundary so visual Skill points and boxes reliably reach the shared SVG
  viewer, while preserving normalized `0..1` evidence geometry.
- Accept explicitly declared pixel annotations using exact captured-frame
  dimensions and report bounded annotation acceptance/rejection diagnostics.
- Use `gemini-robotics-er-2-preview` as the default Robotics-ER model while
  preserving `GEMINI_ROBOTICS_MODEL` as a local compatibility override.
- Reduce visual-annotation label size and weight, and replace the heavy opaque
  outline with a thinner translucent-black halo in both SVG and flattened PNG
  rendering.
- Retry classified transient failures at the read-only VLM boundary, with two
  attempts per backend by default, bounded backoff, and per-attempt provenance.
  Non-transient failures still fall through without repeating the backend.
- Retry a transient camera-frame timeout once at the finite visual Skill's
  capture-only boundary. Camera binding and VLM inference are not repeated,
  no physical action is submitted, and recovered or exhausted retry outcomes
  are projected as SDK-neutral Agent events.
- Allow one validated JPEG, PNG, or WebP user image to accompany turns from
  either Agent view through a bounded Midbrain attachment ID. The OpenAI
  adapter converts the reference to multimodal input while robot-camera Skills
  retain separate live-evidence provenance.
- Present runs in both Agent views as a bounded scrollable conversation
  with one expandable execution summary and independently retained visual
  evidence per turn. Terminal turns survive page reloads for the current
  browser and backend process session; raw chain-of-thought, tool arguments,
  and tool outputs are excluded.
- Request the OpenAI runtime's public automatic reasoning summary for streamed
  runs while retaining SDK-neutral tool and retry progress when a model does
  not emit summary text.

## Agent streaming and visual evidence — 2026-08-02

- Added backend-owned, replayable Agent SSE runs for the regular and developer
  pages, with a versioned SDK-neutral Midbrain event projection and the legacy
  synchronous endpoints retained for compatibility.
- Added exact retained RGB evidence with normalized point/box annotations,
  interactive SVG overlays, deterministic per-annotation colors and editable
  swatches, and color-faithful flattened copy/download. RGB capture now waits a
  configurable bounded interval for the first readable frame after Provider
  activation instead of failing the Agent task during normal camera warm-up.
- Changed the Agent lifecycle boundary so a `HOT` request is not reported as
  complete merely because the Provider accepted the transition. The tool now
  waits for a fresh Manager report showing `HOT` and ready, optionally waits
  for the exact capability that caused activation, and then directs the Agent
  to resume the original finite Skill in the same run. A bounded timeout
  remains visible instead of producing a false activation claim.
- Applied the same readiness wait when the model chooses process-only `START`
  with a non-null `required_capability`. This closes the stopped-camera race
  where the model immediately retried the visual Skill while startup was still
  publishing a degraded heartbeat. Dependency instructions now prefer `HOT`
  even for a stopped Provider because Manager performs startup as part of that
  transition; a timed-out `START` returns an exact `HOT` continuation.

## Unreleased — Canonical monorepo Git workflow

- Allow a newly reviewed stationary workcell calibration to atomically
  supersede the current active calibration instead of requiring expiry or a
  separate revocation. Base-yaw ambiguity now uses the VLM gripper mask plus
  aligned depth to choose the exact 0/180-degree root-Z correction in 3D;
  perspective RGB-arrow review remains a warning fallback only.
- Bound Agent model sessions to the current Test Agent process boot while
  retaining prior SQLite records for audit. A Midbrain restart can no longer
  replay an expired stationary-calibration activation from hidden model
  history; expired and superseded candidates require a fresh calibration
  before Manager is called.
- Made Orbbec camera calibration publication resilient to a restarted World
  State Fabric: the Provider republishes the small static calibration and
  camera transforms every two seconds and only updates its publication cache
  after Fabric acknowledges the observation batch. Reviewed calibration
  activation now verifies exact camera and VIO identity, epoch, convention,
  tracking, readiness, and calibration revision from their recurring Manager
  heartbeats instead of depending on second one-shot Fabric lookups.
- Added spatial convention V2: world/robot +X front, +Y left, and +Z up
  opposite gravity, with legacy Y-up epochs and alignments rejected.
- Changed Local VIO initialization and inertial propagation to Z-up, preserved
  native optical geometry behind explicit `camera_system_x/y/z` names, and
  added a gravity-leveled camera heading frame.
- Added deterministic semantic-direction resolution through timestamped
  world-to-arm transforms or an explicit preview-scoped upright-arm
  confirmation. The arm-mount fallback is independent of VIO.
- Added a separate fixed-rig confirmation, non-resetting VIO readiness check,
  and persisted gravity-aligned before/after effector images. Controller
  completion and visual displacement confirmation are reported independently.
- Replaced global motion inhibit in the fixed-rig check with a bounded
  VIO-local stationary attestation, preventing visual readiness checks from
  fencing Integrated's Basic lease. Added explicit approved `HOT` recovery for
  an Integrated controller already in `RECOVERY_REQUIRED`.
- Made fixed-rig visual depth verification best-effort when an upright-mount
  confirmation already defines the requested direction. Missing exact
  effector depth no longer vetoes IK preview creation.
- Added Agent-facing `POSE_6DOF` translation that preserves the measured
  controlled-frame orientation when requested.
- Exposed the maintained stationary VIO-world-to-arm-base calibration Skill on
  the regular Agent and made post-calibration/post-safe-home Integrated HOT
  recovery explicit.
- Retained rejection of missing, stale, degraded, or
  convention-mismatched evidence outside the explicit arm-mount fallback.
- Simplified stationary base validation to a host-calculated 25-percent
  projected-box size comparison with at most two FoundationPose attempts and a
  categorical VLM front/back decision. Base yaw can change only by an exact
  180-degree rotation about the semantic arm-root +Z axis; base-up and missing
  world-up conditions are warnings rather than transform mutations or vetoes.
- Made the validated stationary-camera pose authoritative for world-to-base
  registration, with live VIO retained only as drift evidence. Removed the
  post-fit support-plane, depth-shift, mount, lever-arm, residual, and VLM
  confidence/orientation gates; if both size attempts miss, the closer result
  is returned with an explicit warning.
- Anchored the developer RGB-D point cloud to that same reviewed stationary
  camera transform while active, preventing live VIO drift from visually
  moving a fixed base relative to its cloud.
- Added the `/dev/spatial-axes` frame inspector and converted world
  point-cloud visualization to Z-up.
- Archived the legacy vegetable-cutting prototype outside active Skills,
  setup, discovery, build, and validation.
- Made workspace startup observation-first: the default now launches only
  Manager and Fabric, opens Midbrain, and ignores Provider
  auto-start entries unless explicitly enabled.
- Wired local signing-secret bootstrap into both setup and bounded startup,
  and made bounded startup reject a Manager that did not receive the reviewed
  workcell activation secret. Agent activation errors now preserve Manager's
  exact secret/camera/VIO diagnostic instead of collapsing to a generic 503.
- Added root-level Windows double-click entrypoints for starting Manager,
  Fabric, and the idle Agent UI service without Providers, and for stopping the
  recorded workspace processes.
- Added confirmed cold-Provider/development-UI activation from component
  boundaries and a guarded whole-workspace shutdown action on the Midbrain UI.
- Added approval-gated Provider lifecycle control to the regular Agent, changed
  Agent approvals from raw SDK JSON to human-readable confirmations, and added
  a preview-then-approve relative IK workflow to both Agent profiles. Approved
  motion now sends the existing one-shot commit and reports the controller's
  bounded terminal completion instead of stopping at target-edit engagement.
- Defined repeated relative wording as another displacement from the current
  measured pose, removed the Agent-facing prior-target retry tool, and exposed
  approval-gated Basic Controller safe-home.
- Raised the configurable Agent SDK turn ceiling from seven to sixteen so a
  bounded inspect, two-Provider activation, preview, and approval workflow can
  reach its final response.
- Added browser-session auto-authorization for exact stationary world-to-arm
  calibration and required necessary Provider/calibration operations to enter
  their real tool approval boundaries instead of ending with conversational
  permission requests.
- Evaluated bounded browser-session authorization in dynamic tool approval
  predicates before invocation, eliminating the approval interruption and
  resume request for eligible Provider activation, exact relative motion, and
  stationary calibration calls.
- Prevented explicit signed world axes from falling back to arm-base mount
  assumptions, and added a labeled world XYZ origin triad plus active VIO frame
  identity to the point-cloud view while retaining gravity/down.
- Made bounded Agent session replay expand to a complete user-turn boundary,
  preventing raw item limits from separating a Responses reasoning item from
  its required function call.
- Changed the regular Agent tool-choice default to `auto`, rotated the GUI Agent
  session keys, and converted an unreachable Integrated Controller preview into
  a structured Basic-then-Integrated recovery route instead of a repeated raw
  connection error.
- Reframed the Manager-hosted Midbrain GUI as the canonical interaction portal,
  added a complete portal operator guide, and moved the superseded point-cloud
  and IMU component-first tutorials into the documentation archive.
- Added a discoverable Relative End-Effector Motion Skill contract with the
  explicit Basic-then-Integrated `HOT` Provider activation sequence.
- Added per-run Agent model, reasoning-effort, and configured VLM selectors to
  both Agent UIs, with Terra and medium reasoning as balanced defaults.
- Fixed Windows extended-path handling that prevented the camera and Basic arm
  development launchers from resolving `$PSScriptRoot`, and made the Midbrain
  shutdown action start `stop_workspace.ps1` directly.
- Changed missing-camera visual Agent calls from raw Fabric 404 failures to an
  actionable `PROVIDER_ACTIVATION_REQUIRED` result.
- Added Manager-hosted systemic liveness and information-rich observation
  pages for Providers and Skills, with manifest-defined UI metadata and an
  explicit confirmation boundary before entering developer surfaces.
- Split the OpenAI Agents SDK browser service into regular and developer
  Agent UIs. The developer Agent can inspect adapter-bound Skills and
  configured Providers, while Provider residency changes use resumable,
  explicit SDK approvals.
- Added a language-neutral component observation-UI contract so Provider
  implementations can migrate independently; future Rust migration should
  prioritize hardware-bound Providers whose SDK and timing requirements are
  comparatively stable.
- Replaced publication-snapshot development with a normal branch-based monorepo workflow rooted in the existing GitHub history.
- Added the supervised vegetable-cutting prototype as a manual-only, non-discoverable experimental Skill while excluding generated plans, runtime captures, logs, environments, and package metadata.
- Expanded repository ignores for local orchestration state, publication copies, variant Rust targets, runtime audit data, replay payloads, and external FoundationPose/SAM2 checkouts.
- Restored explicit Git LFS attributes for the two bundled FoundationPose checkpoint files.
- Added vegetable-cutting compilation, tests, and wheel creation to local validation and its unit tests to hosted CI.
- Preserved the local Windows `mycpp` compatibility implementation as reviewed source instead of leaving it only inside an untracked upstream checkout.
- Replaced the repository-root shared Python environment with component-local
  `.venv` ownership for every Python Provider, Skill, and the Test
  Agent/OpenAI Agents SDK; added isolated setup scripts and a validation guard
  against shared-interpreter regressions.
- Made checksum-manifest generation use Git-controlled inputs so ignored
  virtual environments and external FoundationPose/SAM2 checkouts cannot enter
  release manifests after local setup.
- Limited JSON validation to Git-controlled inputs plus the active local
  Provider registry, preventing ignored third-party runtime checkouts from
  producing false validation failures.

## 0.3.16 — Phase 5 guarded-agent checkpoint (2026-07-29)

- Added formal Agent Skill discovery and large-data route-advertisement contracts. Agent selection follows descriptive Skill metadata, with explicit provider IDs retained as a fallback.
- Kept RGB-D payloads in shared memory while publishing timestamps, buffer references, channel geometry, alignment metadata, and direct Orbbec fallback routes through Fabric.
- Added Manager authorization decisions, lease lineage, denial/invalidation handling, and global safe-termination ownership.
- Moved reusable Cartesian planning, velocity limiting, singularity checks, workspace checks, collision preview, endpoint-jump checks, and command audit into the Integrated Controller.
- Added a durable audit copy of submitted control intent and the controller/provider acknowledgements without routing the latency-sensitive control loop through Fabric.
- Added the Stationary World-Space Arm Alignment, RGB-D Registration, Tool-to-Control-Frame Registration, and General VLM Observation Skills, with browser-based development interfaces following the neutral dark theme.
- Narrowed the OpenAI Agents SDK execution surface to a reviewed decision ID. The model selected the Skill; policy and motion authority remained deterministic and server-side.
- Completed an operator-observed no-contact toilet-paper standoff trial, a measured 12.67 cm vertical lift from the tested pose, safe-home, gravity-float verification, and full service shutdown.
- Preserved error behavior in which loss/fault paths prefer gravity float, while non-error control retains the current mode until a later command.
- Documented remaining Agent SDK roadblocks, component changes, authority lineage, and the unresolved Cartesian-axis/alignment problem under `docs/reference/project_notes`.
- Kept machine-local `config/api_keys.env`, `config/system.env`, active `config/providers.json`, calibration, captures, logs, and runtime state outside the publication.
- Synchronized public configuration templates with blank secret fields, fixed
  an ambiguous Orbbec test import, and added monorepo test source setup.
- Preserved the protected legacy GitHub workflow because the publishing token
  lacked the separate `workflow` OAuth scope. On its Linux host, its exact
  Python command passes 101 tests and skips thirteen: three Agents-SDK-only
  modules plus ten Windows named-shared-memory replay tests. The full Windows
  publication matrix was validated independently.
- Passed the exact stopped `469/469` publication matrix and the wider
  `578/578` local matrix, plus source parsers, Rust formatting, and Rust
  release build.
- Removed the local pre-Phase 1 frozen reference after the operator confirmed
  an external project backup; no runtime source or configuration referenced
  the deleted directory.

## Cross-provider alignment and shutdown hardening (2026-07-26)

- Updated the Stationary World-Space Arm Finder to publish the immutable full `vio_from_camera_reference` pose captured with alignment RGB-D evidence. Downstream fixed-camera Skills no longer need to combine a saved alignment with a later, drifted VIO pose.
- Changed the two-attempt FoundationPose fallback to retain the geometrically better bounded observation when neither attempt reaches the strict VLM threshold, while preserving non-BAD geometry, projection-coverage, orientation, and confidence gates.
- Documented Integrated controlled-frame semantics: `ik_location` names the desired acting-point pose, and Integrated applies `ik_offset` internally. Upstream Skills must not apply the tool offset a second time.
- Added reliable Fabric command staging guidance using producer/boot/sequence identity, `accepted_count`, bounded republishing with fresh sequence and timestamps, and terminal rejection handling.
- Added Integrated workspace-envelope publication, first-command recovery for a measured joint slightly outside its operational range, and acknowledged safe-termination launch behavior.
- Made global Stop All dependency-aware: Integrated is stopped before Basic, safety-critical stop confirmation is required, and powered arm support is preserved when Manager-based safe shutdown cannot be confirmed.
- Added Manager `force_kill_on_stop_timeout` as a per-Provider policy. It remains enabled by default, while the supplied Basic arm registration disables automatic timeout escalation so a missed graceful stop does not silently remove powered support.
- Updated Basic safe-home to revoke and fence the operational lease, clear pending work, and reject late operational commands before the first protective MIT frame.
- Updated formal Contracts to distinguish automatic graceful-timeout escalation from authorized explicit force-stop and to require fencing before Provider-owned protective motion.
- Made repository manifest generation deterministic across Windows and Linux by canonicalizing text to LF before hashing; repaired 33 stale root-manifest entries left by the two implementation commits.
- Exposed the already-published sanitized FoundationPose reBot CAD profile and Base/Gripper reference atlases from the main README. Added a root API-key template and documented why serial-bound calibration, absolute local paths, and camera captures remain excluded.
- Added an identical sanitized FoundationPose runtime/restore profile under `config/foundation_pose`, matching the path used by the supplied Manager configuration so fresh checkouts do not depend on a seeding prompt.
- Added root recovery examples for `system.env` and `providers.json`, made the core initializer create a blank `api_keys.env`, synchronized package fallback templates, and made canonical Provider entries inherit Manager/Fabric endpoints plus the camera mapping name from `system.env`.
- Added a configuration-baseline inventory and automated clean-checkout audit covering generation, preservation, blank secrets, ignore rules, Provider-entry consistency, arm/Skill templates, and FoundationPose runtime references.
- Added active `providers.json` to the publication blocklist so a force-added machine-local Provider registry cannot be uploaded accidentally.
- Recorded the corrective validation and remaining hardware boundary in `BUILD_REPORT.md`. The local vegetable-cutting experiment remains non-deployable and is intentionally not published as a production Skill.

## Stationary World-Space Arm Finder v0.4.0 (2026-07-24)

- Added `skills/stationary_world_arm_alignment` as a finite Skill with an isolated virtual environment, CLI, monitoring GUI, schemas, and regression tests.
- Added the three concrete upstream modes `foundation_base_gripper`, `foundation_base_vlm_gripper`, and `vlm_gripper_only`, plus automatic selection.
- Added upright base correction, projected 3D-box/axis VLM validation, one fresh FoundationPose retry, and three-inference closest-pair voting for large VLM adjustments.
- Added immediate RGB-D shared-memory copying with fresh-bundle retries when a camera `BufferRef` slot is recycled.
- Added result schema version 2 with explicit base/gripper source contracts and separate semantic labels for the FoundationPose gripper model origin and VLM RGB-D foremost-beak point.
- Added on-demand Provider requests and bounded FoundationPose shutdown while retaining camera, VIO, and arm-pose inputs for other consumers.

## reBot arm Providers publication (2026-07-23)

- Added reBot Arm DM Basic Controller 0.1.20 and Integrated Controller 0.7.0 as separate Providers with separate virtual environments.
- Added Manager-discoverable capability readiness and a provider-local operation catalog for upstream Skills.
- Marked MIT one-shot and continuous/HOLD_LB usable.
- Marked POS_VEL one-shot limited to paths at or below 20 cm with no payload or high external load.
- Kept POS_VEL continuous and arm POS_TOR one-shot as experimental/unstable local GUI tests and excluded both from Manager capability discovery.
- Documented Fabric Cartesian target/settings staging, operator Engage + Xbox LB authority, latched MIT/POS_TOR gripper behavior, gravity-float, and authoritative safe-home termination.
- Recorded offline validation separately from physical acceptance; no autonomous motion authority is claimed.

## FoundationPose Provider v0.3.0 publication (2026-07-22)

- Added `providers/foundation_pose` as a Manager-discoverable CAD-based 6D object-pose Provider.
- Added independent Base and Gripper targets for the reBot B601/ER1.6 arm, with camera-relative transforms published into the Fabric.
- Added a GUI-assisted initialization workflow using OpenAI visual localization, two positive object points, cropped SAM2 segmentation, and operator review before tracking.
- Added tested mask refinement defaults: median Lab distance 30 with radius-2 dilation for the Base, and median RGB 10% drift with radius-2 dilation for the neon-green Gripper root.
- Added mesh preparation and renderer tooling, reusable reference renders, prepared-asset caching keyed by source content and preparation settings, and selectable tracking rates.
- Validated 43 Provider tests plus live Manager/Fabric registration and transform publication checks.
- Published the two required FoundationPose checkpoint files through Git LFS with the complete NVIDIA FoundationPose license. These checkpoints are restricted to non-commercial research and evaluation use.
- Kept camera-to-world alignment outside the Provider: the Provider publishes measurements, while a future bounded Skill must aggregate stationary observations, resolve symmetry, and publish an independently authoritative alignment transform.

### Revision 4 packaging corrections

- Applied the remaining Windows-gated `rustfmt` change in the Manager process launcher.
- Changed `scripts/validate.ps1` to run `cargo fmt --all` before the strict formatting check, preventing future platform-gated formatting drift.
- Added `scripts/update_manifests.ps1`; successful validation now refreshes component and repository SHA-256 manifests after formatting and builds.

## 0.3.10 — GitHub source cleanup (2026-07-20)

- Consolidated the Manager, Fabric, contracts, Orbbec Femto Bolt Provider, Local VIO Provider, and Test Agent into one source-only repository.
- Added a canonical documentation sequence and two functional tutorials.
- Added source validation, Python wheel build, GitHub publishing, and CI workflows.
- Corrected Windows PowerShell parsing in `scripts/validate.ps1` by delimiting `${LASTEXITCODE}` before a colon.
- Applied the complete Manager `rustfmt` layout reported by `cargo fmt --all -- --check`, including the process-ID expression missed in revision 2.
- Added the MIT License for original project code and a pending third-party audit notice.
- Declared MIT package metadata for the Rust crates and Python distributions, with the license text included in each Python wheel.
- Retained `Cargo.lock` for reproducible Rust application builds.
- Corrected stale component-version wording in package documentation.
- Excluded machine-local configuration, serial-bound calibration, API keys, proprietary SDK binaries, generated output, runtime state, and unrelated robot-arm Providers.
- Verified 37 Python regression tests and built all three Python wheels.

## Integrated component baseline

- Manager/Fabric 0.3.0
- Orbbec Femto Bolt Provider 0.3.1
- Local VIO Provider 0.2.2
- Test Agent 0.2.9
- Contracts working draft 0.3.9
