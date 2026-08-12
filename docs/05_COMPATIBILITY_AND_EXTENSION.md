# Compatibility and Extension Guide

Midbrain is intended to support different robots, sensors, perception stacks,
and agent frameworks without placing their private APIs in every task. This
guide is the practical entry point for an outside implementation. The
versioned documents under [`contracts`](../contracts/README.md) remain the
authoritative interface descriptions.

## Compatibility model

Compatibility is capability- and contract-based, not directory- or
brand-based. An implementation is compatible when it:

- advertises stable semantic capabilities and schema versions;
- participates in Manager lifecycle, identity, health, and readiness;
- publishes timestamped, provenance-bearing observations through the Fabric;
- uses explicit coordinate frames, convention identifiers, calibration
  revisions, and session epochs where spatial data is involved;
- keeps large payloads behind validated transport references;
- preserves authority, fencing, idempotency, cancellation, and safe cleanup
  when physical resources are involved; and
- returns structured limitations and continuation information instead of
  requiring an Agent to reverse-engineer private failures.

A compatible component does not need to use the same implementation language
or vendor SDK as the reference component.

## Add a Resource Provider

Use a Provider for hardware ownership or computation that benefits from
persistent residency, device ownership, warm state, continuous publication, or
bounded-latency readiness.

1. Select semantic capabilities and schemas. Do not expose only a vendor API
   name when a brand-neutral capability describes the work.
2. Implement the [Resource Provider Contract](../contracts/01_resource_provider_contract.md)
   and applicable lifecycle guidance.
3. Supply a manifest containing identity, version, launch requirements,
   capabilities, dependencies, resource needs, and optional UI metadata.
4. Register with Manager and report process identity, boot identity, residency,
   health, and per-capability readiness.
5. Publish observations through Fabric with source time, arrival time, stream
   sequence, schema version, calibration, frame, validity, and expiry evidence
   as applicable.
6. Use a registered large-payload route for images, point clouds, audio, or
   tensors. Consumers must be able to reject a recycled generation.
7. Make Manager commands idempotent and provide structured failure and retry
   guidance.
8. Implement graceful `HOT` to `WARM` and stop transitions. Physical Providers
   must relinquish authority and reach their defined safe state.
9. Add stopped software tests and the applicable conformance tests before any
   hardware qualification.

Start from the Orbbec Provider for a sensor example, Local VIO for a continuous
compute example, and the reBot Basic Provider for a hardware authority example.
Copy contracts and behavior—not vendor-specific assumptions or local paths.

## Add a finite Skill

Use a Skill for one bounded operation that begins for a purpose, coordinates
capabilities, produces a result, cleans up, and ends.

1. Define one narrow outcome and an explicit input/result schema.
2. Declare required capabilities in the Skill manifest. Discovery must not
   import the implementation or start its dependencies.
3. Bind Providers through Manager rather than embedding a fixed Provider ID
   unless a documented explicit fallback is required.
4. Read coherent observations from Fabric. Do not assemble a private snapshot
   by polling several Provider control APIs for their individual “latest”
   values.
5. Apply a Skill-specific temporal policy using source timestamps, identities,
   epochs, uncertainty, and expected cadence. A single global stale timeout is
   not sufficient for all robotic work.
6. Separate read-only evidence, nonphysical preview, authorization, and
   physical commit.
7. Bound retries to operations that are nonphysical, idempotent, or proven not
   to duplicate an uncertain physical result.
8. Return structured success, rejection, limitation, and continuation data.
9. Release leases, sessions, model resources, and Provider residency changes
   in guaranteed cleanup paths.

The [Finite Skill Contract](../contracts/07_skill_contract.md) defines the
lifecycle boundary. The Skills under `skills` show manifest discovery,
read-only perception, initialization, alignment, and guarded-execution
patterns.

### Keep replaceable-effector geometry in profiles

Visual arm Skills should not hard-code one gripper, final-joint length, tool
offset, landmark material, point count, VLM description, or reference-image
choice into otherwise reusable alignment mathematics. The
[`refine_arm_root_translation`](../skills/refine-arm-root-translation/SKILL.md)
Skill demonstrates the profile boundary:

- Basic publishes the active Provider-owned mounted-effector profile through
  `robot_arm.assembly_state`; the Skill must follow that selection and must not
  maintain a second private effector selector;
- strict core fields retain kinematics, controlled frames, collision geometry,
  inertia, and actuator ownership for all mounted-effector consumers;
- the optional namespaced
  `extensions.midbrain.skill.refine_arm_root_translation.v1` object owns only
  this Skill's timing policy, landmark descriptions, point sets, aggregation,
  offsets, and reference-image policy;
- each visual landmark declares one through eight physical point names. Every
  declared point must be detected and registered; only the arithmetic mean of
  the complete 3D point set is accepted;
- the default bare-gripper landmark is a non-reflective proximal rail midpoint,
  while the controller point remains the gripper tip;
- the blade profile instead describes its two knife-handle endpoints and owns
  its independent controlled-frame offset; and
- replacing the gripper or fixed tool requires a new mounted-effector profile
  revision with its own attachment and landmark relationship, not a
  conditional inside the generic solver.

The mounted-effector core remains closed to unknown ad hoc fields, while the
`extensions` map is deliberately open to namespaced object-valued additions.
Known extension namespaces are validated by the shared schema; unknown
namespaces are preserved for their owning modules. This lets Basic use an
effector when the VLM aligner is absent, and lets an effector omit the alignment
extension when it is not qualified for visual refinement.

Store both controlled-frame-to-landmark and landmark-to-controlled-frame
vectors and validate that they are exact inverses. Rotate those vectors with
timestamped controlled-frame FK; never reinterpret them as world or arm-base
axis offsets. Profiles may change the landmark description when a tip becomes
reflective or occluded without changing the refinement algorithm. Reference
images are profile-swappable policy but currently marked `FUTURE` until asset
resolution is implemented. Optional physical fiducials belong in separate
profile revisions and must not become an implicit baseline requirement.

## Connect another Agent framework

The Agent is a planner and coordinator, not a privileged device driver. A new
Agent adapter should:

1. Read the Manager capability and finite-Skill catalog.
2. Present only eligible, typed Skills to the model or deterministic planner.
3. Keep Provider activation, exact preview validation, authority, and
   controller enforcement in Midbrain host paths.
4. Map framework-specific run, message, tool, approval, and error signals into
   the [Agent Event Stream](../contracts/15_agent_event_stream.md).
5. Project visual results through the
   [Visual Evidence Contract](../contracts/16_visual_evidence_and_annotations.md)
   instead of exposing private filesystem paths or raw tool output.
6. Keep user attachments distinct from current robot-camera observations.
7. Treat browser disconnect, model timeout, cancellation, and process restart
   as explicit states. None of them proves whether a physical action completed.
8. Preserve one stable run identity and structured terminal result.
9. Pass only an opaque preview or plan identifier through the model when the
   host already owns canonical execution state. Resolve authorization limits,
   targets, timing, controller digests, and freshness checks from that pending
   host state immediately before execution.

A compatible Agent adapter may reduce one mechanically determined
preview-to-commit model round trip with a call-scoped prepared-action
projection. It must prepare nonphysically, retain canonical state behind an
opaque identifier, bind that state to the exact agent tool-call identity,
evaluate the existing authorization policy after preparation, and fail closed
if the binding is missing or changed. Only one explicitly configured
continuation pair may be coalesced. Do not recursively interpret
`required_next_tool`: lifecycle recovery, calibration activation,
re-observation, replanning, user questions, and other branches retain their
own owning workflow and authorization boundary.

When a task-facing Provider declares dependencies in Manager configuration,
the adapter requests that Provider once. Manager resolves, orders, and
deduplicates the transitive dependency graph. An Agent may inspect a typed
failure after a bounded activation fails to converge, but it must not turn
`A depends on B depends on C` into three routine model-selected lifecycle
steps. This keeps dependency topology out of Agent prompts and permits another
compatible Provider graph to be substituted without rewriting conversation
logic.

Treat the resulting compound operation as one Agent-facing decision boundary,
not as one indivisible implementation API. Its internal Provider, Skill, and
controller calls remain typed, timeout-bounded, observable, and attributable
to their owning components. An adapter must return control when a continuation
changes owner or requires semantic interpretation, recovery, reviewed state
activation, new evidence, operator input, or uncertain-outcome handling.
Independent Skills must not be collapsed merely to reduce the visible tool
count.

For current reference workflows, the generic host infrastructure now provides
the intended readiness and exact-continuation handoff mechanics. Further
latency or reliability work should normally be implemented by the owning
Provider or Skill while preserving the same capability, evidence, and result
contracts. A compatible Agent should not need custom prompts that replay a
Provider dependency graph or a Skill's deterministic internal state machine.

An adapter may use an OpenAI, Google, local, deterministic, ROS-connected, or
other planning runtime. Manager, Fabric, Provider, Skill, safety, and evidence
contracts must not depend on that framework's private classes.

## Add a robot or arm implementation

A new robot integration normally separates:

- a hardware-facing Provider that owns transport, measured state, command
  validation, fencing, watchdogs, and safe relinquish;
- an optional planning/control Provider that owns kinematics, trajectories,
  collision policy, preview, and execution progress; and
- finite task Skills that express goals and verify results.

Publish the robot's frame convention, controlled frame, action point, joint
and workspace limits, supported control modes, capability maturity, and
physical qualification evidence. Do not infer compatibility from matching
axis names or a successful simulation.

All cross-frame motion must follow the
[Spatial Frame Convention](../contracts/14_spatial_frame_convention_v2.md) and
the [Timestamped Transform Graph](../contracts/06_timestamped_transform_graph.md).

## Portability rules

- Do not hard-code the repository's absolute workspace path.
- Keep active machine configuration and secrets outside tracked source.
- Do not make Windows named shared memory the semantic contract; it is one
  transport implementation behind BufferRef semantics.
- Do not make HTTP or browser presentation the semantic contract; schemas and
  lifecycle behavior are the compatibility boundary.
- Do not make one camera, arm, model, or agent SDK the default meaning of a
  general capability.
- Keep optional proprietary SDKs, model checkpoints, and license-restricted
  assets separately installable and clearly attributed.
- Report unsupported and degraded modes explicitly.

## Provider documentation and terminology

A Provider README is its human and installation-agent landing page. It must
link every retained package-local document, but a Provider does not need a
fixed set of empty or one-paragraph files. Create a separate safety,
validation, architecture, calibration, or legal document only when that topic
has enough independent authority or workflow to justify one.

Keep information with one owner:

- manifest and schemas own machine-readable identity, versions, capabilities,
  fields, enums, streams, and readiness declarations;
- cross-component contracts own interoperability semantics;
- Provider architecture owns implementation-specific state and responsibility
  boundaries;
- safety documents own operator-visible invariants and failure behavior;
- validation documents separate stopped, simulated, guarded physical, and
  unqualified evidence;
- changelogs summarize historical outcomes rather than restating the current
  manual; and
- the active roadmap owns promotion, qualification, or future-design work.

For coding agents, explicitly map aliases to canonical identifiers. A UI label,
vendor name, and API enum may describe one mechanism without being valid
substitutes in code. Likewise, do not treat “arm root,” “arm base,” “tool
frame,” “controlled frame,” “TCP,” and “action point” as synonyms unless a
versioned transform or schema says so. Preserve exact enum, capability, stream,
frame, and policy strings in implementation work; use prose aliases only after
their mapping is stated.

## Compatibility review checklist

A proposed component is ready for integration review when:

- its manifest and schema files parse and use versioned identifiers;
- lifecycle, restart, cancellation, and dependency behavior are tested;
- per-capability readiness is distinguishable from process liveness;
- observation identity, timing, calibration, and coordinate metadata are
  complete;
- large-payload generation and producer-restart failures are tested;
- all state-changing operations are idempotent or explicitly non-repeatable;
- physical authority loss reaches a defined safe result;
- software validation can run without activating hardware;
- hardware validation has a separately authorized scope and acceptance record;
- limitations and next actions are machine-readable where an Agent must react;
  and
- the component README links to its contracts instead of restating them.

## Contract maturity

The contract set contains working drafts at different maturity levels. The
[contract index](../contracts/README.md) lists each document and status. An
outside integration should pin the exact contract and schema versions it was
tested against, reject incompatible major versions, and negotiate optional
capabilities rather than assuming every Provider implements the complete
reference stack.
