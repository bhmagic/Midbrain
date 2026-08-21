# Robot assembly and free-space motion contract

## 1. Scope

This contract separates the installed robot assembly from the Providers and
Skills that operate it. It defines the static selection of the arm model,
assembly calibration, mounted effector, collision geometry, and control roles
qualified for one physical robot instance.

The active assembly selection is machine-local. Reusable profiles remain in
the package that owns them. Installing another arm Provider should therefore
require selecting its Provider-owned profiles, not copying them into a central
profile directory.

## 2. Assembly selection ownership

The active assembly selection lives under `config/robot_assemblies`. It selects
one Provider ID and Provider-root path. The arm model, calibration, mounted
effector, and collision geometry are then relative references under that root,
each with an expected identity and revision and an optional SHA-256 digest.

Paths are resolution hints. Profile identity, revision, and digest are the
compatibility boundary. A resolved profile whose declared identity or content
does not match the selection must be rejected before physical readiness.

An arm-model profile may contain a flexible `appendix` object for namespaced
consumer data such as arm-base CAD and visual-reference selection. Field names
and JSON value shapes inside that appendix are intentionally open. Each
consumer validates only the namespace it owns, while the arm Provider preserves
and publishes unknown appendix entries without acquiring the consumer's duties.
The assembly-selected arm model remains the selector; Skills must reject stale
local appendix content that does not match the active assembly fingerprint.

The resolved assembly state includes:

- Assembly ID and revision
- Arm resource and Provider identity
- Robot model identity and revision
- Arm-model appendix data for bounded consumers
- Calibration identity and revision
- Mounted-effector identity and revision
- Collision-geometry identity and qualification
- Qualified free-space, contact, and grip control roles
- A canonical digest over the resolved selection

## 3. Static effectors and runtime attachments

A mounted gripper or fixed knife is part of the static assembly selection. A
tool or object held by a gripper is runtime attachment state and must not
rewrite the static assembly configuration.

A mounted-effector profile may mark model joints as `inactive_joint_names`.
Those joints belong to neither the arm command group nor an active effector
actuator group. This represents a fixed tool replacing an actuated gripper
without incorrectly turning the unused gripper motor into a seventh arm joint.
The Basic Provider must also treat those joints as physically unavailable: it
must not register, enable, poll, mode-switch, or command their motor endpoints.
Its legacy fixed-width joint state may retain an explicit synthetic slot, but
that slot must be marked `INACTIVE_NOT_INSTALLED`, excluded from freshness
qualification, and unavailable to every root or group lease. Active actuator
groups plus inactive joints must account for the complete legacy model without
overlap.

A mounted-effector profile may also contain an optional `extensions` map for
namespaced consumer configuration. Core mounted-effector fields remain strict;
unknown extension keys must be namespaced and their values must be objects.
The shared schema validates extensions it knows, while a consumer validates
and owns only its namespace. Other consumers preserve unknown extensions and
must not acquire the extension owner's duties. Therefore a missing visual-
alignment extension does not invalidate Basic's mounted-effector selection; it
only makes that alignment Skill unavailable for the selected effector.
The separate `midbrain.skill.locate_arm_base.v1` extension may declare one
coarse visual orientation landmark for Locate Arm Base. It owns only eligible
point names, the VLM description, arm-base frame, and controlled-frame offset;
it does not transfer Basic's FK ownership or the translation-refinement
Skill's registered-depth, all-points, timing, or confidence policy. Locate Arm
Base may use a generic controlled-frame-origin fallback when this optional
extension is absent, but Basic still treats the mounted effector as valid.

Changing a mounted effector invalidates existing previews, authorizations,
tool registrations, payload assumptions, collision geometry, and controller
qualification. Runtime attachment changes invalidate any preview that bound a
different attachment revision.

Mounted-effector inertia is expressed in an explicit `reference_frame`, which
must be the selected attachment child frame. The arm collision profile owns the
arm geometry: collision capsules bind their ordered radii to
`polyline_point_frames`, and capsule `i` joins the origins of point frames `i`
and `i+1`. The mounted-effector profile owns its sphere, capsule, or box
`collision_primitives` in named effector frames. Replacing the gripper or fixed
tool therefore replaces its geometry through the same profile selection rather
than requiring an arm-profile rewrite. Primitive dimensions must be positive,
transforms must be finite, IDs must be unique, and all frame references must
resolve inside the selected assembly.

A collision consumer must declare and validate the geometry forms it supports.
It must fail closed when a selected profile contains a nonempty primitive set
that it cannot evaluate. The current reBot Integrated consumer evaluates the
arm capsule polyline and mounted-effector spheres expressed in the selected
controlled frame. It rejects arm-owned frame primitives and non-sphere
effector primitives until those evaluators are implemented.

## 4. Controller separation

Free-space, contact, and grip control are separate roles:

- Free-space control owns collision-aware transit and prohibits intentional
  contact.
- Contact control owns bounded interaction trajectories and force behavior.
- Grip control owns gripper closure, hold, slip detection, and release.

Free-space and contact control are mutually exclusive owners of the arm command
group. Grip control may eventually operate concurrently on a separate gripper
command group. The Basic hardware Provider remains the final authority for
transport, limits, fencing, watchdogs, and safe relinquish.

## 5. Free-space goal ownership

A free-space Skill submits one complete Cartesian goal. It must not decompose a
multi-axis request into separate axis moves. The free-space Provider owns frame
resolution, orientation-constraint resolution, IK, route selection, collision
checking, trajectory generation, and progress reporting.

Supported goal classes may include:

- Absolute or relative translation
- Absolute pose
- Relative axis-angle or quaternion rotation
- Measured-orientation preservation
- Controlled-axis alignment with a declared remaining-twist policy
- Combined translation and orientation

The reference `move_effector_to_world_point` finite Skill accepts one absolute
controlled-effector-origin point in the active V2 world convention. It may
bind the exact source world-frame ID and VIO session epoch; either mismatch
invalidates the request before preview. Resolution requires the current
reviewed rigid world-from-arm-base transform because an upright-mount rotation
attestation cannot resolve an absolute translation. The host converts the
point once, preserves the measured controlled-effector orientation with
`POSE_6DOF`, and submits one complete Integrated goal. The Agent must not read
the current pose or synthesize a relative displacement for this operation.

Every Agent-initiated physical move requires an immutable preview followed by
a signed, short-lived, policy-specific, one-time commit. Autonomous free-space
policy may issue that assertion without a human dialog. A preview binds the
measured start, assembly revision, mounted-effector revision, runtime
attachment revision when present, transform revisions, scene revision, target,
constraints, and final-state policy.

The scene revision is optional. When fresh scene evidence is unavailable, the
preview and commit bind a `null` scene revision and explicitly expose degraded
scene-blind operation. A fresh scene, when available, remains authoritative for
collision rejection.

The current temporary semantic policy ignores `PUSHABLE`, adds 10 mm clearance
to `KEEP_OUT`, and adds zero extra clearance to `WORK_OBJECT` while still
prohibiting geometry intersection. General obstacle rerouting is not currently
implemented. The controller evaluates the direct Cartesian path; if it is
blocked, an executable partial result must stop at its closest collision-free
boundary and explicitly report that the requested goal was not reached. It
must never reinterpret the remaining motion as contact work.
Endpoint language such as "reach", "touch", "until reaching", or "until
touching" is a boundary-seeking free-space request under this contract, not
contact authorization and not a reason to refuse the move. An explicit request
for sustained force behavior such as pushing, pressing, cutting, scraping, or
gripping belongs to the separate contact or grip role.

The Provider must re-read the current Basic assembly fingerprint before preview
and immediately before commit. A mismatch invalidates the preview and must not
be repaired by silently rebinding during the same physical authorization.

Manager may expose installed compatible effector profiles in the main UI, but
it selects only an exact Provider-owned profile identity/revision. Static
selection changes require confirmation that the physical effector matches and
must be rejected while Basic or any transitive dependent Provider is loaded.
The selection and any profile-content edits take effect only after those
Providers restart; a live gravity model must never be silently rebound.

## 6. Configuration and controller policy

Intrinsic model, calibration, mounted-effector geometry, and collision geometry
belong to revisioned profiles. Command rate, timing, trajectory sampling,
controller gains, and execution watchdogs remain controller policy unless the
Basic hardware Provider must enforce them as a hardware safety ceiling.

A higher controller may request a value below a Basic ceiling, but it must
never weaken the Basic ceiling.

## 7. Compatibility

The initial reBot assembly may use adapters for the existing version-1 model,
calibration, fixed-tool transform, and Integrated link-radius policy. These
legacy references must be labeled by qualification and must not be presented as
general arm compatibility.

Future arm Providers may supply their own model, calibration, effector, and
collision profiles without changing the free-space Skill contract.
