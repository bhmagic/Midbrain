# Integrated Provider safety boundary

Integrated plans and coordinates free-space arm motion. Basic remains the
final hardware authority for motor modes, load-bearing stiffness and damping,
joint/rate/effort limits, lease fencing, command deadlines, gravity support,
and safe-home. Read [Basic safety](../../rebot_arm_dm/docs/SAFETY.md) before
physical use.

Low force, low torque, low stiffness, or slow movement is not inherently safe.
In particular, insufficient load-bearing impedance can allow the arm to fall
before gravity support stabilizes.

## Physical authority

Every physical move requires an exact controller-owned plan followed by a
signed, short-lived, policy-specific, one-time commit. The commit revalidates:

- Provider boot and configuration identity;
- selected assembly fingerprint and controlled-frame geometry;
- measured joint start and requested position/orientation goal;
- joint, motor, timing, singularity, and workspace limits;
- global motion inhibit and the fenced Basic arm-group lease; and
- the newest usable semantic scene when available.

Normal free-space Skills use autonomous host policy authorization and do not
pause for human approval. A nonphysical plan, Agent statement, scene update,
or Provider activation never grants motion authority by itself.

## Collision and completion

The collision model combines assembly-profile arm capsules with every sphere
in the selected mounted-effector profile. The scene compiler uses the same
profile geometry to remove robot/tool cells from semantic output, and the main
3D viewer renders those same effector spheres.

`PUSHABLE` is ignored under the current temporary policy. `WORK_OBJECT` has
zero additional clearance but may not intersect the robot. `KEEP_OUT` receives
10 mm additional clearance. Integrated evaluates the direct Cartesian path;
general obstacle rerouting is not implemented. A blocked route may execute
only its closest collision-free prefix and must report `CLOSEST_SAFE`.

Wording such as “reach,” “touch,” or “until reaching” identifies a no-contact
boundary destination. It does not authorize intersection and is not a reason
to refuse movement up to that boundary.

Completion telemetry distinguishes elapsed command duration, measured target
arrival, and closest-safe partial arrival. Callers must not infer success from
a deadline alone.

## Contact, grip, and attachments

Integrated exposes no intentional-contact, torque-baseline, gripper, manual
target-staging, or teleoperation API. Cutting, pushing, pressing, scraping, and
gripping belong to separately qualified controllers and Skills.

A future grip controller may hold the gripper resource while Integrated holds
the arm resource. Moving the arm then also requires a runtime attachment
revision describing the held object's transform, payload, and swept collision
geometry. Until that ingestion path exists, free-space motion with an
undeclared held object is prohibited.

The selected static effector inertia comes from the assembly profile and Basic
uses it for gravity feed-forward. Replacing or retuning the tool invalidates
the profile qualification.

## Fallback and termination

Transport uncertainty, stale physical feedback, lost lease, motion inhibit,
Basic fault, changed assembly, invalidated measured start, collision, or
execution error blocks new commands and requests gravity float when authority
is still valid. Integrated does not automatically retry a physical move after
a safety fallback.

The developer page retains only gravity-float and safe-terminate controls.
Safe terminate launches the authoritative PowerShell helper and requires an
accepted launch acknowledgement; launch acknowledgement does not prove
safe-home completion.
