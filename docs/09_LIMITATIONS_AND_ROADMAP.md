# Current Limitations and Roadmap

This document contains active gaps and priorities only. Completed work belongs
in changelogs; dated physical evidence belongs in validation records and Git
history; implementation details belong in the owning component or active
design.

## Current system boundary

Midbrain demonstrates an agentic robot workflow with managed Providers,
timestamped shared state, finite Skills, semantic perception, guarded arm
motion, visual evidence, and one replaceable Agent adapter. It is still a
development system, not a safety-certified autonomous robot or hardened remote
operator platform.

Natural-language physical work remains constrained by deterministic preview,
authority, fencing, controller validation, semantic-scene checks, and
post-action evidence. Capability maturity varies by Provider and installed
hardware.

## P0: trustworthy spatial autonomy

### Complete movement-based arm-root alignment

The non-moving `refine_arm_root_translation` Skill is implemented. It can use
one to five timestamp-coherent VLM/RGB-D/FK observations to improve XYZ while
preserving a previously trusted rotation, but one observed 3D point cannot
establish a new six-degree-of-freedom transform.

The current before/after collector can gather useful gripper correspondences,
but the ordinary automatic movement-based alignment workflow is not complete.
Implement the versioned observation set, rigid solver, conditioning checks,
candidate activation, rollback, finite calibration motion, visualization, and
negative tests specified in
[Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md).

FoundationPose remains an explicit finite initializer and compatibility route;
it must not become an automatic fallback for generic alignment.

### Stabilize arm-FK and Fabric transform history

Repeated live translation refinements succeeded before later FK preflights
remained approximately 98 ms and 408 ms ahead of available transform history,
with one timestamped lookup returning 404. One HOT recovery succeeded earlier
in the same run sequence, but later HOT requests did not prove that the
underlying FK publisher or Fabric ingestion resumed. The refinement Skill
correctly failed closed and applied no update. Investigation then reproduced a
Fabric-side amplification mechanism: every graph query decoded and sorted the
full retained history for every edge while holding the shared store read lock,
and multi-link arm queries could delay publication write-lock acquisition.

The current stabilization candidate indexes typed transform observations at
ingestion, performs graph traversal after releasing the store lock, acquires
the write lock once per publication batch, and lets the refinement Skill use
the existing transform route's optional event-driven bracket wait. It preserves
the ordinary 256-sample stream history, 4096 transform samples per edge,
intermediate FK samples, and existing conflict/provenance behavior.

Before considering the anomaly closed, instrument raw joint/FK publication
and Fabric edge ingestion separately with
provider boot/process identity, sequence, source timestamp, arrival timestamp,
requested capture-window end, and explicit 404 reason. Then run a bounded
ten-minute soak test with and without intervening motion. Do not weaken
zero-unqualified-extrapolation policy or infer stationarity from publisher
silence. See the
[VIO and arm-FK timestamp anomaly handoff](../skills/refine-arm-root-translation/references/vio_and_arm_fk_timestamp_anomaly_handoff.md).

### Enforce fresh semantic scenes during execution

The arm scene compiler publishes a canonical short-lived semantic sphere scene.
The remaining boundary is to prove that expiry, producer loss, transform or
self-filter revision change, and newly occupied clearance prevent or stop an
authorized transit as intended.

Scene semantics must remain upstream-described. Visible material, color, or
location must not silently turn every object into an obstacle. Close-range
geometry should refine only the selected workpiece or relevant obstacle.

### Generalize the no-contact closed loop

The reference loop can locate an opaque workpiece and effector, preview a
scene-aware correction, execute a bounded leg, and reobserve. It still needs
broader qualification for:

- target ambiguity and multiple similar objects;
- thin, reflective, transparent, and depth-poor surfaces;
- changing or partially occluded scenes;
- increasing residual and uncertain physical outcomes;
- long-run transform refinement; and
- reliable stopping at the declared no-contact boundary.

Bearing-only evidence must not be promoted to collision geometry. Active
perception moves require the same preview, authority, and verification path as
other motion.

## P1: reproducibility and outside compatibility

### Deterministic recording and replay

Add a hardware-incapable recording format for synchronized RGB, native and
registered depth, IR, IMU, calibration and transform revisions, clock domains,
Provider identities, semantic scenes, previews, authority, execution outcomes,
and Agent events.

Replay should exercise the same consumer contracts while being structurally
unable to command hardware. It is required for estimator comparison,
fault-injection, regression diagnosis, and third-party conformance.

### Provider and Skill conformance

Turn the working contract set into executable compatibility checks for:

- lifecycle and per-capability readiness;
- dependency activation and restart;
- BufferRef generation and producer replacement;
- observation timing, frames, calibration, and epochs;
- cancellation, idempotency, and structured failures;
- authority loss and safe relinquish; and
- finite-Skill cleanup and result contracts.

An outside implementation should be able to prove compatibility without using
the reference vendor SDK or touching physical hardware. See
[Compatibility and Extension](05_COMPATIBILITY_AND_EXTENSION.md).

### Normalize package and contract versions

Several packages currently expose inconsistent versions across `VERSION`,
manifest, Python metadata, and changelog files. Establish one machine-readable
owner per package and add validation that rejects drift. Contract maturity and
schema compatibility must remain separate from the repository release number.

## P2: faster autonomous execution

### Reduce Agent turns without weakening boundaries

Instrument intent interpretation, discovery, Provider activation, evidence
collection, planning, authorization, execution, and result interpretation.
Move frequent deterministic sequences into bounded compound Skills or
controller-owned orchestration after the Agent resolves intent.

This optimization must preserve individual typed Skills for outside adapters
and recovery. It must not hide preview, authority, controller, or evidence
boundaries.

### Controller-owned multistep routing

Allow one high-level goal and task policy to produce a complete, immutable
scene-aware route. Integrated should own per-leg arrival, clearance, bounded
local replanning, and terminal hold or release while goal, selected object,
contact policy, scene lineage, and authorization remain unchanged.

### Safe autonomous recovery

Extend automatic retry only where action identity, idempotency, completion
evidence, and uncertain-outcome handling prove that the retry cannot duplicate
a physical action. Recovery should return actionable continuation data rather
than generic failure text.

## P3: physical qualification

### Qualify or retire Integrated CONTACT_WORK

Integrated currently implements `CONTACT_WORK` as an attended, GUI-only,
one-shot `POSITION_EFFORT_LIMITED` endpoint with a separately captured torque
baseline and explicit joint or wrench budgets. It is experimental, is absent
from Agent capability discovery, and is not a general Cartesian-force
controller.

Before promoting it beyond hardware characterization:

- measure torque signs, bias, friction, gravity/payload-model error, and
  transport-loss behavior across representative postures;
- validate baseline rejection, effort-budget mapping, physical-ceiling
  saturation, stopping, and gravity-float recovery on the installed arm;
- establish uncertainty bounds before interpreting joint residuals as
  end-effector contact or Cartesian force;
- qualify gripper and arm behavior with representative tools and workpieces;
  and
- either define a stable, discoverable contact capability with deterministic
  authority and result semantics or retire the experiment.

Until those gates pass, current implementation details and attended checks
belong only in the Integrated Provider's architecture, safety, and physical
test documents.

The installed arm profiles still require broader measurement of:

- arrival, overshoot, oscillation, and stopping behavior;
- payload, reach, speed, acceleration, jerk, and thermal derating;
- serial loss, Manager loss, lease expiry, and emergency release;
- collision and newly occupied clearance behavior;
- compliant hold and position-lock disturbance response;
- cross-axis behavior for representative camera and arm mounts; and
- contact-capable control before any workpiece-contact autonomy.

Raw motor or SDK maxima are not whole-arm autonomous operating limits. Current
ordinary motion and experimental contact/continuous profiles must remain
separately advertised and qualified.

VIO and object-pose qualification also still require synchronized ground truth,
trajectory error, outage drift, reacquisition discontinuity, camera/IMU time
offset, mask perturbation, symmetry, occlusion, repeatability, and long-duration
tests.

## P4: security and field operation

Before any command interface is exposed beyond loopback, add:

- authenticated identities and role-based command authority;
- TLS or an authenticated local gateway;
- origin and CSRF protection for browser commands;
- rate limits, request identity, and security logging;
- encrypted and policy-controlled chat, journal, attachment, and visual
  evidence retention; and
- tamper evidence, export, deletion, redaction, and incident-review policy.

Low-interaction field missions require local mission limits, independent stop
mechanisms, bounded offline behavior, authenticated steering, and explicit
rules for loss of network, perception, operator signal, or upstream authority.

## Important implementation limits

- The Local VIO backend is a Python reference inertial-first ESKF, not a
  feature-level MSCKF or fixed-lag optimizer.
- BufferRef pinning and general subscriptions are incomplete.
- Recording/replay is not yet a complete repository-wide facility.
- Provider containment, restart backoff, and stale-state invalidation need
  broader fault-injection.
- The Agent UI and local journal are development diagnostics, not authenticated
  command or field-audit systems.
- FoundationPose measurements depend on reviewed masks, CAD geometry, depth,
  symmetry, and the separately installed GPU runtime.
- Obstacle-route search and contact-capable control are not generally
  available.

## Completion policy

When a roadmap item is implemented, move its lasting interface rules into the
owning contract and component documentation, record the release in the
changelog, and remove the finished task narrative from this file. Do not append
dated run journals here.
