# Validation

Midbrain separates stopped software validation, live nonphysical validation,
and explicitly authorized physical qualification. Passing one layer does not
imply that a later layer is safe or complete.

## Repository validation

From Developer PowerShell in the repository root:

```powershell
.\scripts\validate.ps1
```

The validation entry point checks clean configuration baselines, parses source
and configuration, runs Python and Rust tests, builds package artifacts in a
temporary location, checks Rust formatting, and refreshes checked manifests as
configured by the script. CameraHost builds only when valid Orbbec SDK paths
are supplied.

The Python phase and GitHub's `python-tests` job both invoke
`scripts/run_python_validation.py`. That runner is the single source for
dependency bounds, editable local packages, source paths, compilation roots,
test directories, and wheel packages. CI must not duplicate those lists in
workflow YAML. This parity ensures that contract packages such as
`midbrain-bufferref-client` and newly added Providers or Skills are installed
and tested in both environments.

Run the configuration audit independently with:

```powershell
.\scripts\test_config_baselines.ps1
```

Component suites that have separate environments or optional dependencies
retain their own entry points, including:

```powershell
.\providers\foundation_pose\scripts\validate_publication.ps1
.\providers\rebot_arm_dm\scripts\verify.ps1
.\providers\rebot_arm_integrated\scripts\verify.ps1
.\providers\rebot_arm_contact\scripts\verify.ps1
.\providers\rebot_arm_grip\scripts\verify.ps1
.\skills\contact_work_runtime\scripts\verify.ps1
.\skills\refine-arm-root-translation\scripts\check.ps1
```

Consult each component's `VALIDATION.md` for its current coverage and
limitations. Test counts belong in generated output and changelogs, not in
evergreen framework documentation.

## Evidence classes

| Class | May use live devices | May move hardware | What it proves |
|---|---:|---:|---|
| Static/source | No | No | Parsing, schemas, packaging, manifests, and policy invariants |
| Recorded replay | No | Structurally impossible | Deterministic consumer behavior and injected failures |
| Live nonphysical | Yes | No | Device data, timing, calibration, transforms, perception, and previews |
| Guarded physical | Yes | Only explicit bounded scope | Installed-system behavior for the exact authorized test |
| Deployment qualification | Yes | Controlled test program | Repeatability, faults, environment, payload, duration, and operating envelope |

Every physical report must identify the installed hardware, configuration and
calibration revisions, software commit, authority scope, exact motion envelope,
acceptance criteria, observed result, terminal safe state, and remaining
limitations.

## Core live acceptance

### Camera and VIO

- Provider identity, boot, calibration, streams, and timestamps are coherent.
- Initialization reaches a current convention-versioned epoch.
- IMU propagation continues between visual updates.
- Visual correction can age or fail without freezing the state estimator.
- Reset creates a new epoch and old-epoch observations are rejected.
- Point-cloud visualization never mixes epochs.

### Fabric and transforms

- Recycled BufferRefs, obsolete producer boots, and mismatched generations are
  rejected.
- Timestamp-nearest and synchronized queries obey caller tolerance.
- Frame direction, multiplication order, convention, calibration, and epoch
  are explicit.
- Revoked or superseded transforms cannot authorize a new physical preview.

### Agent and Skills

- Discovery does not import or start Skill implementations.
- A manifest-declared external Skill host remains generic; Skill-specific
  hardware/VLM code and Python dependencies remain inside the Skill package.
- Dependencies become ready before the finite operation runs.
- Browser disconnect does not duplicate or silently cancel backend work.
- Read-only retries cannot repeat a physical action.
- Visual evidence refers to the exact analyzed frame and preserves provenance.
- Structured results distinguish success, rejection, limitation, uncertain
  outcome, and actionable continuation.

### Limited Graph and compact context

- The concise Agent authoring projection compiles deterministically into the
  canonical immutable graph and fails before execution on invalid children,
  inputs, compact pointers, routes, reachability, nesting, or limits.
- Direct and graph Skill calls validate complete results, expose only declared
  compact values plus an opaque detail reference, and never make detailed
  diagnostic storage authoritative for the action outcome.
- Provider-detail and Skill-result-detail reads remain top-level, read-only,
  sanitized, and unavailable as graph children.
- A child-declared Provider handover uses the same Manager lifecycle policy and
  resumes the same child call without moving Provider selection or credentials
  into the graph.
- Each physical child preserves the direct call's prepared-action,
  authorization, fencing, freshness, completion, and uncertain-outcome checks.
- Failure routing publishes bounded compact `last_failure`, and no authoring-
  correction path can repeat a started graph or physical child.
- Child visual evidence reaches the event stream when produced and remains
  attributable to the exact graph, node, and child call.
- Wall time, transitions, physical actions, per-node timeout, and retry counts
  remain bounded; graph executors cannot be nested.

The current reference implementation is accepted as near stable for its
retained linear physical workflow. Retained live branch/switch/retry/model-
route coverage, simultaneous multi-visual presentation, material-cut outcome
sensing, and strict session-context sizing remain separate qualification; see
[Limited Graph Status and Qualification](14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md).

### Arm-root translation refinement

- An active motion-usable alignment and matching camera, VIO, arm, convention,
  frame, calibration, and effector-profile identities are required.
- The default bare-gripper path consistently resolves the two rail endpoints,
  averages their registered 3D points, and rotates the profile's 80 mm
  rail-center-to-controller-tip offset with controlled-frame FK.
- Synthetic sign and rotation tests prove that the offset follows controlled
  +X rather than world or arm-base X, including a rotated gripper pose.
- One to five samples produce one arithmetic-mean correction and at most one
  Manager state update. Adoption zero is observation-only; adoption one is the
  full accepted XYZ correction.
- The proposed rotation is byte-identical to the active rotation. Manager
  rejects stale revisions, identity changes, rotation changes, inconsistent
  adopted deltas, and Fabric publication failure.
- Invalid exact depth permits one VLM reselection. A sufficiently large raw
  correction requires marked-image VLM review; failed or unresolved review,
  profile delta limits, and unbracketed FK all leave alignment unchanged.
- Live qualification should repeat refinements for at least ten minutes with
  and without intervening arm movement while recording raw arm-FK and Fabric
  history cadence. Current timestamp anomalies are documented in the
  [VIO and arm-FK handoff](../skills/refine-arm-root-translation/references/vio_and_arm_fk_timestamp_anomaly_handoff.md).

### Robot control

- Basic remains the final hardware command, fencing, watchdog, and safe-state
  boundary.
- Integrated previews without moving and commits only an exact authorized
  preview.
- Contact Work remains a separate Provider: it accepts only an exact
  Skill-signed plan, commands Basic directly in `POSITION_EFFORT_LIMITED`,
  streams Contact-owned Cartesian segments at Basic's advertised 50 Hz control
  cadence, chains replacements from the prior commanded setpoint, retains the
  mode guard across moves, and relaxes on explicit cleanup or timeout when no
  carry is confirmed.
- Sequential segment IK knots stay within the configured 2 mm translation
  spacing, retain requested orientation and hard locks, and produce changing
  joint targets rather than repeated submission of one final endpoint.
- Null Agent profile selectors resolve the live persisted defaults, while an
  explicitly requested profile number remains exact. Slicing retract resolves
  its signed negative-blade displacement from measured effector position at
  move acceptance rather than correcting toward a stale planned endpoint.
- Contact tests do not use low-torque or low-stiffness load-bearing states.
- Contact Provider disposition and joint telemetry do not claim that an
  intentionally unreachable Cartesian endpoint succeeded.
- Shared Contact runtimes start a signed stage dwell only after matching
  `trajectory_complete` evidence. Grip dwell begins only after stable grip
  contact, and neither elapsed dwell nor contact inference proves task success.
- The Grip Provider owns the gripper-group 50 Hz stream, new-grip thermal gate,
  torque-only contact inference, and runtime attachment identity. A normal
  failed grip verifies functional opening, enters MIT float, relaxes Contact,
  returns an explicit unsuccessful result, and creates no carry.
- Carry confirmation requires matching Contact/Grip carry and attachment
  identities plus `POSITION_EFFORT_LIMITED` on every active joint. Ordinary
  carried motion stays in Contact; release opens before gripper float and arm
  relaxation. Lay Flat exposes its initial Integrated/FLOAT interval as an
  explicit exception rather than claiming uninterrupted all-joint hold.
- The Slicing numeric developer surface freezes a resolved plan before motion,
  resolves one relative begin point from a captured effector origin, derives
  the slice endpoint and measured-start-relative outward displacement, and
  rejects Contact until Integrated
  completes in verified `FLOAT` with the same workcell calibration binding and
  no more than the documented actual-pose handoff drift. It then requires an
  observed Integrated `WARM` state with no active trajectory or Basic lease
  before Contact activation.
- Scene, authority, lease, transform, and controller identities remain valid
  through execution.
- Arrival is measured rather than inferred from command submission.
- Authority loss, timeout, process loss, and shutdown reach the defined powered
  safe state.
- Safe-home and complete safety-ordered shutdown are confirmed after a test.

## Qualification still required

The current repository does not constitute localization, collision, robot,
functional-safety, or field-operation certification. The active qualification
gaps are maintained in
[Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md).

Historical phase reports and completed task handovers are retired from the
active tree. Use component changelogs and Git history when a past acceptance
record must be reconstructed.
