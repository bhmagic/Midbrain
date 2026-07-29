# OpenAI Agents SDK Physical-Control Roadblocks

Date: 2026-07-29
Workspace: `testing_physical_ai_testing_pose_cutting_vegie`
Status: end-to-end no-contact physical test completed

## Executive result

The OpenAI Agents SDK is able to select and invoke a Midbrain physical Skill.
The completed test used an `Agent` and `Runner` with one eligible
`FunctionTool`, `execute_reviewed_observation_motion`. The only model input was
an already approved `decision_id`. The model did not receive or choose a
Cartesian target, joint target, speed, contact permission, controller mode,
lease operation, safe-home operation, or fallback motion.

The successful execution was:

- Decision: `6fb800ec-5b4b-4242-80ee-f0744c7f6cc8`
- Integrated plan: `a47557e6-ea33-4862-a3b9-b6790baee963`
- Authorization assertion:
  `6f6cd56c-b6e8-4908-ab93-95c64eb6a0ee`
- Scene: `toilet-roll-20260729T0815Z-agent-sdk`
- Target behavior: position the calibrated gripper front 0.10 m above the
  registered solid top rim of the toilet roll, with no contact
- Planner: Integrated Controller, `DIRECT`, collision-free
- Modeled minimum clearance: 0.07943 m
- Controller limits: 0.25 rad/s maximum joint speed and 0.05 m/s effective
  Cartesian speed
- Execution: 40 of 40 stages completed
- Controller state when execution completed:
  `HOLDING_AUTHORIZED_TRANSIT_ENDPOINT`
- Final measured controlled-frame error: 0.001185 m
- Final measured vertical standoff: 0.099942 m
- Accepted authorized-transit count: 1
- Rejected authorized-transit count: 0
- Controller fault/error: none

The SDK was not the main source of difficulty. The principal blockers were
runtime provenance, short-lived observations, short-lived controller previews,
shared-memory reference lifetime, service-launch ambiguity, calibration
activation lifetime, and the need to keep the final physical tool extremely
narrow.

## Safety boundary that worked

The final boundary has four distinct stages:

1. A read-only Skill produces fresh spatial evidence.
2. Integrated Controller owns path planning and produces a nonphysical preview.
3. The host records a separate, exact-preview-bound operator authorization.
   Approval does not execute.
4. The Agents SDK may select one decision-ID-only tool. The host adapter
   revalidates the stored decision, refreshes the exact reviewed semantic
   scene, mints a one-time signed assertion, and commits only the stored plan
   and digests.

This design prevents a language model from broadening a physical action after
review. A model-generated tool argument can name only the decision. All
motion-bearing values come from the immutable authorization record and the
Integrated preview.

## Roadblock inventory

### 1. Workspace source import could resolve to a different checkout

Symptom:

- Test Agent behavior did not always match the files in this workspace.
- Python could import packages from `C:\Projects\testing_physical_ai` rather
  than this cutting-development copy.

Root cause:

- The launcher did not construct an explicit workspace-local `PYTHONPATH` for
  every provider and Skill source tree.

Resolution:

- `test_agent/scripts/run.ps1` now enumerates this workspace's provider and
  Skill Python roots and puts them ahead of ambient installations.

Residual risk:

- Ad-hoc Python commands still need the same source-path discipline.
- A clean packaging/publication flow should eventually remove editable-source
  ambiguity.

### 2. Manager and Fabric release binaries could be older than their source

Symptom:

- Runtime API behavior differed from current Rust source and tests.
- A source change could appear ineffective after a workspace restart.

Root cause:

- The launcher used existing release binaries without proving that they were
  built from the current source state.

Resolution during this test:

- Manager and Fabric were rebuilt and current binaries were launched.

Resolution after the test:

- `platform_core/scripts/run_workspace_bounded.ps1` now compares the Manager
  and Fabric release executable timestamps with the applicable workspace Rust
  sources, crate manifests, root Cargo files, toolchain file, and build
  scripts. It rejects startup with an explicit rebuild instruction when the
  source is newer than the executable.
- The updated PowerShell launcher passed a parser check. A complete clean
  launcher restart remains part of pre-publication regression testing; it was
  not performed while the live arm stack remained powered and supporting the
  post-test pose.

Residual risk:

- Timestamp comparison detects the stale-build failure encountered here but
  does not cryptographically bind an executable to a source commit. Embedding
  a build identity remains the stronger long-term solution.

### 3. Required HMAC signing secrets were absent

Symptom:

- Workcell activation and physical execution assertions could not be verified.
- Initial calibration activation returned HTTP 409 after a review record had
  already been persisted.

Root cause:

- Clean local configuration did not bootstrap
  `MIDBRAIN_REVIEW_AUTH_SECRET` and the execution-authorization signing secret.

Resolution:

- Added `platform_core/scripts/ensure_local_signing_secrets.ps1`.
- Updated root, Platform Core, and Test Agent environment templates.
- Secrets are local and ignored; tooling does not print their values.

Residual risk:

- Production secret lifecycle, rotation, and multi-host trust are not solved by
  the local development bootstrap.

### 4. PowerShell execution policy could prevent the environment loader

Symptom:

- Dot-sourcing `common.ps1` in the current shell failed.
- The activation helper then ran without the expected secret and Manager
  rejected the signed request.

Root cause:

- Windows execution policy differed between the parent shell and the intended
  launcher environment.

Resolution:

- Run the helper inside an explicit child PowerShell with
  `-ExecutionPolicy Bypass`, then import `system.env` and `api_keys.env` before
  starting Python.

Residual risk:

- Ad-hoc operator commands remain easy to run in the wrong PowerShell context.
- A single bounded first-party activation launcher is preferable.

### 5. Review persistence and activation were not one retry-safe transaction

Symptom:

- The helper persisted an approved review decision before Manager activation.
- If activation failed, a retry with a new nonce or review idempotency key
  conflicted with the stored decision.

Root cause:

- Review creation and Manager activation are deliberately separate, but the
  helper did not make their retry identity explicit enough.

Resolution:

- Added stable CLI arguments for review request ID, activation request ID, and
  nonce.
- Improved Manager error reporting to include the HTTP response body.
- Successful retries reused the exact review ID and nonce.

Required follow-up:

- Persist a small local activation-attempt record before the first network
  call and automatically reuse its IDs and nonce.
- A duration validation error should occur before the review decision is
  persisted.

### 6. Calibration and Integrated Controller had an unsafe startup order

Symptom:

- Starting Integrated before stationary calibration caused its Basic lease to
  be revoked by the calibration's global motion inhibit.
- Integrated entered `FAULT_FLOAT/RECOVERY_REQUIRED` even though no target had
  been submitted.

Root cause:

- Calibration correctly inhibits all motion providers. Integrated correctly
  treats unexpected lease loss as a fault. Starting both in the opposite order
  created a predictable conflict.

Resolution:

- Stop Integrated before stationary calibration.
- Complete calibration and release the inhibit.
- Activate the reviewed transform.
- Start Integrated afterward and then apply the calibrated controlled-frame
  offset.

Verified result:

- Applying the controlled-frame offset produced zero Basic submissions.

### 7. Manager stop/start registration has a short lifecycle race

Symptom:

- An immediate start after stop could return `already_registered` even though
  the provider process was already gone.

Root cause:

- Process exit, provider deregistration, and Manager lifecycle state do not
  become visible atomically.

Mitigation:

- Use bounded status polling and retry start only after deregistration.

Required follow-up:

- Manager should expose `STOPPING` and complete stop only after registration
  cleanup, or make start idempotently wait for the preceding stop generation.

### 8. Fabric HTTP latency exceeded a two-slot RGB-D BufferRef lifetime

Symptom:

- Fabric sometimes returned a synchronized bundle in 90-150 ms rather than
  approximately 1 ms.
- By the time a consumer mapped the referenced camera slots, a two-slot ring
  had already recycled them.
- Failures appeared as expired/recycled `BufferRef` errors even though the
  camera was healthy.

Root cause:

- Large RGB-D payloads correctly live in shared memory, but the reference
  retention budget was smaller than observed metadata-query latency.

Resolution:

- Fabric remains the source of route identity, timestamps, mapping metadata,
  synchronization policy, calibration, and small numeric/text data.
- RGB-D consumers may use the provider-declared shared-memory mapping to read
  the latest slots when the exact Fabric refs expire.
- The fallback copies registered depth first and RGB second, verifies
  generation/boot identity, and enforces the provider-declared timestamp delta.
- Results explicitly report
  `PROVIDER_SHARED_MEMORY_LATEST_REF_FALLBACK`.

Verified result:

- 1920x1080 RGB and registered depth, approximately 30.1 Hz.
- RGB/depth timestamp delta 1097 us in the validation capture.
- Boundary IoU 0.998798 and valid-depth fraction 0.610716.

Residual risk:

- Two slots remain fragile under CPU stalls.
- A larger ring or an explicit pin/copy lease would reduce fallback frequency.

### 9. The Test Agent opened the same shared-memory mapping more than once

Symptom:

- Aligned-depth and RGB copies could race each other and observe different
  generations.

Root cause:

- The capture path created separate readers/mappings for related payloads.

Resolution:

- Reuse one reader/mapping and copy aligned depth before RGB.
- Added a one-reader ordering regression test.

### 10. Local VIO could stop publishing fresh body pose while reporting tracking

Symptom:

- `localization.body.pose` sequence stopped advancing.
- Pose age grew past the spatial Skill's 500 ms limit.
- VIO status still described the tracker as `TRACKING`, with an expired
  BufferRef recorded only as a subordinate error.

Root cause:

- Local VIO fetched the Fabric RGB-D bundle in its main loop and then attempted
  to read already recycled short-lived refs. The outer exception prevented new
  pose publication.

Resolution:

- Local VIO caches small Fabric route metadata.
- It reads the latest aligned-depth/RGB and IR/native-depth references through
  its existing persistent shared-memory reader.
- It retries a bounded number of times and independently checks timestamps.

Verified result:

- New VIO epoch:
  `aed3c599-6934-4147-86c5-1e98a0cd99f0`.
- Body-pose sequences advanced continuously.
- Sampled ages were 58-439 ms despite intermittent Fabric delay.
- VIO returned to `HEALTHY/TRACKING`.

### 11. Freshness belongs to each consuming Skill

Symptom:

- A request for “fresh Fabric data” was initially treated as if Fabric should
  decide one universal freshness window.

Why that is wrong:

- The same timestamp can be acceptable for one consumer and stale for another.
- VLM/DL inference time is created inside the Skill after capture.
- Fabric should remain passive and fast: preserve source/receipt time and
  history, but do not redefine data freshness for every use.

Resolution:

- Each Skill evaluates source timestamp, receipt timestamp, transport delay,
  association, hard expiry, and its own completion age.
- Spatial registration rejected a 1181 ms body pose at its 500 ms policy limit.
- The repaired run accepted body-pose evidence at 149-397 ms.

### 12. Calibration review windows were easy to exhaust during agent pauses

Symptom:

- A valid calibration process exited because a reviewed-file response did not
  arrive within 180 seconds.
- One timeout happened while the Codex task was compacted.

Root cause:

- The external review handshake and agent conversation lifecycle are not the
  same clock.

Mitigation:

- Use a 600-second bounded review window for interactive local review.
- Poll artifacts in 45-second or shorter intervals.
- Keep process waiting and log reading separate.

Required follow-up:

- Expose review-request state through an API or durable task queue instead of
  relying on a detached process plus filesystem polling.

### 13. Buffered detached logs made successful completion look stuck

Symptom:

- A polling shell exceeded its outer timeout even after the calibration child
  had completed.
- PowerShell process redirection often returned no PID text to the caller.

Root cause:

- Detached child stdout buffering, PowerShell `Start-Process` redirection, and
  a wrapper that mixed waiting with log tailing.

Mitigation:

- Poll only process/PID or artifact existence with a hard deadline.
- Read the completed log in a separate short command.
- Never run the unbounded workspace launcher in the foreground.

### 14. Calibration candidate and activation lifetimes were too short for debugging

Symptom:

- A reviewed candidate remained valid for 15 minutes, but repeated integration
  debugging consumed most of it.
- Manager activation is capped at five minutes.
- One otherwise valid preview inherited only 15 seconds and expired before
  authorization.
- Manager correctly rejected overlapping activation refresh.

Root cause:

- Candidate, activation, observation, preview, authorization, and assertion
  lifetimes are nested but were operated manually across many tool round trips.

Resolution for the test:

- Generate a new calibration.
- Start Integrated only after activation.
- Perform fresh registration, scene staging, preview, authorization, and SDK
  invocation in one bounded host workflow while preserving their logical
  separation.

Required follow-up:

- Add an orchestrator that understands all nested expiries and refuses to
  begin when the remaining budget is insufficient.
- Consider a longer development-only candidate lifetime while retaining short
  physical preview/assertion lifetimes.

### 15. A five-minute activation cannot be refreshed while it is active

Symptom:

- Manager returned HTTP 409:
  `an active workcell calibration must be revoked or expire before another is activated`.

Interpretation:

- This is correct one-active policy, not a Manager failure.

Required follow-up:

- The orchestrator should wait for expiry, explicitly revoke with authority, or
  finish the operation inside the active lifetime. It must not repeatedly call
  activate and treat the conflict as transient.

### 16. A controller preview initially expired before the SDK tool ran

Symptom:

- The first real Agent SDK execution call failed with
  `approved decision or controller preview has expired`.
- Controller counters remained at zero commits/submissions.

Important evidence:

- The Agents SDK had already selected
  `execute_reviewed_observation_motion`.
- The host adapter rejected the stale record before it issued an assertion.

Root cause:

- Human/tool round trips consumed much of the 30-second preview TTL.

Resolution:

- The final workflow created a fresh preview, recorded the existing user grant,
  and immediately invoked the SDK in one bounded host operation.

### 17. Semantic-scene freshness was shorter than normal model latency

Symptom:

- Integrated requires the exact semantic scene to be current at commit.
- Default scene freshness is 1000 ms, while a remote model turn normally takes
  longer.

Root cause:

- A static reviewed scene revision and its transport receipt age were treated
  as the same concern.

Resolution:

- The authorization record contains the exact semantic scene payload.
- The narrow execution adapter validates its revision against preview
  authority and re-stages that exact scene immediately before assertion
  issuance and commit.
- The model cannot supply or change the scene.

Residual risk:

- The current scene is a conservative toilet-roll `KEEP_OUT` sphere, not a
  general environment reconstruction.
- Future perception should publish richer current geometry while preserving
  exact revision binding.

### 18. A broad motion tool would give the model too much authority

Risk:

- A compound tool accepting target, speed, mode, contact, lease, safe-home, or
  fallback behavior would let model output become a physical-control policy.

Resolution:

- Added `execute-reviewed-observation-motion`.
- Its only input is `decision_id`.
- Host code retrieves every physical value from the reviewed record.
- The final commit includes exact plan ID, request digest, preview digest,
  decision ID, and one-time assertion digest.

### 19. SDK-native approval pause/resume is not yet the system authority

Observation:

- The OpenAI Agents SDK has tool-approval concepts, but this project already
  has a richer browser authorization record tied to controller identity,
  preview digests, scene revision, resolver, and expiry.

Current decision:

- `FunctionTool.needs_approval` remains false for this narrow tool.
- The tool is callable only after the host authorization record is already
  `APPROVED`.
- The adapter revalidates the host decision and controller authority.

Required follow-up:

- If SDK pause/resume is adopted, it must carry the same exact Midbrain
  decision identity rather than creating a second independent approval truth.

### 20. The SDK model is not the image-review mechanism in this workflow

Observation:

- The user required local Codex multimodal inspection, not a separate external
  image API call.

Resolution:

- Codex reviewed the exact RGB/depth/calibration artifacts locally.
- The Agents SDK received only the final decision ID for execution selection.
- Image understanding and physical commit authority remain separate.

### 21. Test Agent authorization state is process-local

Symptom:

- Restarting Test Agent clears pending decisions and their one-time execution
  state.

Why it matters:

- Code reload was safe only before the final authorization was created.

Required follow-up:

- Persist authorization records and consumed assertion IDs in an append-only
  local store or Manager-owned service.
- Recovery must preserve one-time semantics and reject replay after restart.

### 22. Launcher commands could appear stuck indefinitely

Symptom:

- Foreground invocations of `run_workspace.ps1` did not return because they
  supervise long-lived processes.
- Wrapping the same call in a stopwatch did not create a timeout.

Resolution:

- Use the bounded launcher or detached provider lifecycle APIs.
- All polling and external calls used explicit hard timeouts.
- Progress updates were emitted between bounded waits.

Required follow-up:

- The main launcher should clearly identify itself as a long-running
  supervisor.
- The bounded launcher should be the default agent entry point.

### 23. Runtime JSON output can be too large for reliable operator inspection

Symptom:

- Full Manager provider state and controller planning output produced tens of
  thousands of lines.
- PowerShell converted file-content strings into verbose provider objects when
  JSON serialization was used incorrectly.

Mitigation:

- Query and print only fields needed for the current decision.
- Read log tails as plain strings.
- Store full evidence as files and show compact identities/hashes in chat.

### 24. The workspace root has an empty `.git` directory

Symptom:

- `git status --short` returns:
  `fatal: not a git repository`.
- The root `.git` directory exists but contains no entries.

Impact:

- Change enumeration, diff review, commit, and push cannot safely run from the
  development root.
- A publication staging directory exists, but runtime code must not link to or
  import from that hard-copy/staging subtree.

Required follow-up before upload:

- Establish which directory is the authoritative Git worktree.
- Recreate or restore Git metadata without overwriting source.
- Produce a clean publication diff from the authoritative root.
- Verify no runtime configuration references the hard-copy/publication folder.

### 25. A later authority-loss event ended the retained endpoint hold

Observation after documentation and focused tests:

- The authorized transit had already completed 40/40 stages and reported
  `HOLDING_AUTHORIZED_TRANSIT_ENDPOINT`.
- A later read-only state query reported the retained transit as `RELEASED`
  with `platform availability or motion authority lost`.
- Integrated was `DEGRADED`, `ready=true`, `engaged=false`, and
  `control_state=TARGET_EDIT`.
- Basic remained connected and `HEALTHY`; the local Basic lease was still
  owned and renewing, and gravity-support telemetry remained available.
- No command, release, float, mode switch, lease switch, or safe-home request
  was sent during this observation.

Interpretation:

- This does not invalidate the SDK commit result: the commanded transit
  completed before the later authority-loss transition.
- Authority/platform loss is an error condition, so leaving the normal
  retained-endpoint execution state is consistent with the current policy that
  an error should fall back to the least-damaging gravity-supported behavior.
- It does show that a completion snapshot cannot be used as an indefinite
  current-state claim. Current controller, Basic, lease, and support telemetry
  must be queried separately.

Required follow-up:

- Reproduce the transient Manager/Fabric/authority loss with timestamped
  lifecycle logs after the operator authorizes another powered test.
- Confirm deterministically that the error path enters the intended
  gravity-supported state and that the controller's latched `DEGRADED` state
  has an explicit, observable recovery procedure.
- Do not treat this report as authorization to recover, move, float, safe-home,
  change mode, change lease, or shut down the arm.

### 26. Natural-language Cartesian axes are not yet unambiguous

Symptom:

- Human instructions use physical terms such as "up" while camera world,
  arm-base, effector, tool, and object frames use different axis components.
- In this workcell, physical vertical mapped primarily to positive world `Y`
  and positive arm-base `X`.
- A correct transform exists, but a natural-language direction still needs an
  explicit semantic frame and gravity reference before it becomes a target
  vector.

Evidence:

- A requested 20 cm final upward lift exceeded the configured positive
  controlled-frame workspace boundary by about 3 mm.
- Nineteen centimeters was rejected for singularity, IK residual, joint jump,
  endpoint joint travel, and aggregate travel.
- Fifteen and fourteen centimeters were rejected by endpoint joint-travel
  policy.
- Thirteen centimeters passed all preview gates. The measured displacement was
  approximately 12.67 cm before the controller classified
  `DEADLINE_FLOAT_BEFORE_ARRIVAL` and confirmed gravity-float.

Impact:

- The completed Agent SDK observation transit remains valid.
- It is not evidence that generic words such as "up", "front", or "left" can
  safely map to a fixed raw axis in every installation.
- Hard-coding the current camera/base relationship would create a latent
  frame-dependent motion error.

Required follow-up:

- Carry semantic direction, source frame, target frame, transform revision,
  timestamp, gravity provenance, and uncertainty through planning.
- Validate translated and rotational axis signs and scales with small
  separately authorized motions after calibration changes.
- Keep controller workspace, singularity, IK, collision, speed, and arrival
  gates authoritative.
- See `CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md`.

## Physical-test evidence

Pre-commit:

- Fresh registered target:
  `[0.0069377033, -0.4888948820, 0.6864028467]` m in
  `world/stationary_camera/20260729T080635Z-7286b758`
- Pixel: `[y=775, x=930]`
- Depth: 0.8250 m
- Valid samples: 49 of 49 in the robust-median patch
- The solid top rim was selected; the center hole was excluded.
- Workcell activation:
  `d8a59f4f-2583-433a-9852-78fd7129c962`
- VIO epoch:
  `aed3c599-6934-4147-86c5-1e98a0cd99f0`

Controller preview:

- Proposed world point:
  `[0.0069377033, -0.3888948820, 0.6864028467]` m
- Proposed arm-base point:
  `[0.5241232042, -0.0184842779, 0.1917891188]` m
- Strategy: `DIRECT`
- Collision-free: true
- Minimum modeled clearance: 0.07943 m
- Contact permitted: false

Final measured state:

- Controlled frame in arm base:
  `[0.5229408828, -0.0184151082, 0.1917394834]` m
- Controlled frame in active world:
  `[0.0068328666, -0.3889533542, 0.6875821356]` m
- Position error from planned controlled frame: 0.001185 m
- Vertical standoff from registered roll point: 0.099942 m
- Final joint command/measured mismatch remained below 0.00019 rad in the
  controller completion record.

Fresh post-motion artifacts:

- `test_agent/screenshots/rgbd_qc_rgb_20260729T082008_795411Z.jpg`
- `test_agent/screenshots/rgbd_qc_depth_20260729T082008_795411Z.png`
- `test_agent/screenshots/rgbd_qc_composite_20260729T082008_795411Z.jpg`
- `test_agent/screenshots/rgbd_qc_rgb_20260729T082008_795411Z_gamma025.jpg`

The post-motion RGB frame was underexposed. A deterministic gamma-only review
copy was used without changing pixel geometry. The gripper and roll remain
visibly separated. Metric controlled-frame evidence is the stronger
verification and reports a 9.994 cm standoff.

## Tests added or exercised

- RGB-D one-reader ordering regression
- Provider latest-reference fallback and timestamp checks
- Local VIO direct latest-reference regression
- Decision-ID-only Agent SDK tool schema
- Ineligible-tool exclusion for the motion driver
- Reviewed decision and exact preview identity validation
- Exact semantic-scene revision refresh before assertion issuance
- Scene mismatch rejection before assertion issuance
- Focused reviewed-execution suite: 3 passing tests after the scene-refresh
  change
- Earlier focused Agent discovery/RGB-D suites: 20 passing tests

## Publication-readiness resolution

Completed for this upload:

1. Prepared the update in an isolated clean clone of `bhmagic/Midbrain`; the
   local development root still is not treated as an authoritative Git
   worktree.
2. Passed the full local stopped matrix at `578/578` (548 Python and 30 Rust
   tests), including the local-only cutting prototype. Passed the exact
   GitHub candidate at `469/469` (439 Python across 62 files and 30 Rust
   tests) after excluding that prototype.
3. Passed Rust formatting/release build, clean-configuration, Python
   compilation, JSON parsing, and PowerShell parsing.
4. Audited every Git publication candidate for active config filenames,
   key-shaped credentials, private keys, ignored runtime/upstream trees, and
   oversized non-LFS artifacts. Local API keys and active configuration are
   excluded.

Remaining engineering work, but not represented as completed by this upload:

1. Make calibration review/activation retries one durable transaction.
2. Add an expiry-budget orchestrator for calibration, observation, preview,
   authorization, assertion, and model latency.
3. Persist Test Agent authorization and one-time assertion state.
4. Repeat the bounded launcher stale-binary preflight when lifecycle changes
   require it; do not restart hardware merely for publication.
5. Resolve the open Cartesian direction/alignment contract before treating
   natural-language axes as general autonomous motion input.

## Final post-report progress

- The later authority-loss transition was reviewed from confirmed
  gravity-float.
- A bounded 13 cm-class lift was executed within the controller's valid
  envelope; larger targets were rejected rather than overridden.
- Authoritative Basic safe-home completed.
- Test Agent, all Providers, Manager, Fabric, and controller services stopped.
- Final endpoint, listener, and process checks found no remaining Midbrain
  workspace service.

## 21. Hosted CI cannot yet exercise the complete Agent SDK graph

The repository's protected GitHub Actions workflow predates the Phase 5
component graph. Updating a workflow file requires the separate GitHub OAuth
`workflow` scope; the publishing token had repository write access but not
that scope. GitHub therefore rejected the attempted workflow-file update, and
the existing workflow was deliberately preserved instead of bypassing the
protection.

The legacy workflow installs neither the OpenAI Agents SDK nor Google GenAI
and exposes only the earlier Python source roots. SDK construction tests must
skip before importing SDK-only dependencies in that environment. The exact
legacy Python command passes 111 tests and skips three SDK-only modules. It is
not evidence for the full Phase 5 Agent graph.

The complete clean publication candidate was validated independently: 439
Python tests across all 62 published test files plus 30 Rust tests, for
`469/469`. Until the workflow is updated through an identity with `workflow`
scope, release documentation must report hosted legacy-CI coverage and the
independent publication matrix separately.
