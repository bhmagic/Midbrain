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
