# Setup and Operation

This is the canonical operator path for the current Windows reference system.
Component development and physical qualification require the additional
instructions beside each Provider.

## Requirements

The complete camera and VIO path targets Windows 10/11 and requires:

- Developer PowerShell for Visual Studio 2022 with the C++ workload;
- stable Rust MSVC with `cargo` and `rustfmt`;
- Python 3.11;
- CMake;
- Orbbec SDK 2.8.6 development and runtime files; and
- an Orbbec Femto Bolt.

The optional reBot arm path also requires the supported seven-motor assembly,
reviewed machine-local calibration, Windows serial access, and
the reviewed MotorBridge 0.5.1 freshness build installed by Basic's setup
script. The Xbox-compatible controller is needed only for the
manual Integrated development GUI.

The optional FoundationPose route requires an NVIDIA/CUDA/PyTorch environment,
the pinned upstream runtime, Git LFS checkpoints, and any separately installed
initialization dependencies. Its third-party license restrictions apply.

The Orbbec SDK, MotorBridge, upstream FoundationPose runtime, virtual
environments, API keys, and measured device calibration are not distributed as
ordinary repository source.

## Workspace and local state

The scripts support any normal writable workspace path. In examples,
`<MIDBRAIN_ROOT>` means the repository root; do not copy a developer-specific
absolute path into configuration or documentation.

Machine-local state belongs under `config` and ignored component runtime
directories. Do not commit API keys, signing secrets, serial identities,
measured calibration, captures, logs, SQLite runtime records, or active
Provider configuration.

## First setup

Open Developer PowerShell in the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup builds Manager and Fabric, creates component-owned Python
environments, builds CameraHost when its SDK paths are available, installs the
core Providers and Skills, and creates missing local configuration from blank
examples without overwriting existing files.

Manifest-discovered Skills may declare a Skill-owned setup entrypoint. Test
Agent setup runs those entrypoints inside each Skill directory; the
arm-root-translation refiner therefore keeps its numerical runtime and
dependencies in `skills/refine-arm-root-translation/.venv` instead of adding
them to the Agent environment.

There is no repository-root Python environment. Each Python process launches
from the environment owned by its component.

Set up the arm Providers separately:

```powershell
.\providers\rebot_arm_dm\scripts\setup.ps1 -WithMotorBridge
.\providers\rebot_arm_integrated\scripts\setup.ps1
.\providers\rebot_arm_contact\scripts\setup.ps1
.\skills\contact_work_runtime\scripts\setup.ps1
.\skills\slicing\scripts\setup.ps1
.\providers\rebot_arm_dm\scripts\register.ps1
.\providers\rebot_arm_integrated\scripts\register.ps1
.\providers\rebot_arm_contact\scripts\register.ps1
```

Restart Midbrain after registering a Provider or changing the eligible Skill
list. Manager snapshots Provider configuration and Provider UI manifests at
startup, and the Agent snapshots its eligible execution adapters at startup.
Registration does not start the Contact Provider; its entry remains
`auto_start=false` until a task requests `HOT`.

After restart, open the Slicing card in the Manager portal or navigate to
`http://127.0.0.1:8000/dev/skills/slicing`. This developer surface accepts one
absolute active-world or current-effector-relative begin point, world blade and
slicing directions, and a slice length. It derives the slice endpoint, planned
outward endpoint, and measured-start-relative retract displacement. The page
can also append/delete mounted-effector blade-use
profiles, including hard joint locks, and Skill-owned load/retract/timing
profiles. Any profile can be deleted, saving fills the lowest missing positive
number, and any remaining profile can be selected as the store default. The
next Agent invocation live-loads a changed Slicing blade profile or default
after verifying the active effector identity; no workspace restart is needed.
Preparation freezes and
displays the resolved plan without moving. Stage 1 is the Integrated alignment;
Stage 2 remains server-disabled until Stage 1 reaches verified `FLOAT`, then
Manager moves Integrated to `WARM` and the Skill verifies its Basic lease has
been released before activating Contact. Stage 2 then streams the three
Contact-owned Cartesian segments at Basic's advertised control cadence and
sends terminal relax through the Slicing Skill's signed path. The FLOAT
handoff allows bounded drift from its captured measured pose. The surface
bypasses language interpretation, not Manager, Integrated, Contact, Basic,
calibration, or authorization boundaries.

The strict Agent tool carries nullable blade and motion profile selectors. It
must send null unless the user explicitly names a profile number; null resolves
the live defaults at invocation time. Engage and slice are absolute targets.
Retract is the signed negative-blade displacement resolved from measured
effector position when Contact accepts the third move, so prior endpoint error
does not redirect extraction.

Set up the retained FoundationPose paths only when required:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\skills\stationary_world_arm_alignment\scripts\setup.ps1
```

These commands make the explicit finite initializer and compatibility Provider
available. They do not make FoundationPose the default alignment path or an
automatic fallback.

## Start Midbrain

For normal operation, double-click `Start Midbrain.cmd`. It starts Manager,
Fabric, and the idle Agent UI, then opens:

`http://127.0.0.1:7001/`

The portal is observation-first. Opening it does not activate hardware,
execute a Skill, reset a spatial epoch, or authorize motion. Providers remain
`COLD` until a guarded operator or Agent workflow requests them.

For automation or recovery:

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Useful options include `-NoBrowser`, `-StartAgentUi`, `-CoreOnly`, and
`-AllowProviderAutoStart`. Normal desktop startup deliberately ignores old
machine-local `auto_start: true` entries unless that last option is supplied.

## Use the portal

Check Manager and Fabric before relying on component state. Provider cards
distinguish:

- process liveness;
- `COLD`, `WARM`, and `HOT` residency;
- component health;
- per-capability readiness;
- observation freshness and source identity; and
- active work.

A running or `HOT` Provider can still be unready, unhealthy, stale, or missing
an optional capability. Open its observation page before entering a
development UI.

Development UIs may expose administrative or physical controls beyond an
ordinary Agent workflow. The portal presents a warning and guarded activation
step before opening them. Finite-Skill development links may start an
inspection host; they do not execute the Skill itself.

## Use the Agent

The regular and developer pages are two projections of one backend-owned
autonomous Agent runtime. The developer view adds diagnostics; it does not add
authority or bypass controller checks.

For a supported physical action, the intended boundary is:

1. inspect current Manager, Provider, and Fabric state;
2. make required Providers ready through lifecycle policy;
3. collect coherent evidence;
4. produce a nonphysical controller preview;
5. resolve policy or a development authorization for that exact preview;
6. execute through the controller's guarded commit path; and
7. report success only from bounded controller completion and required
   post-action evidence.

Closing an SSE connection or browser tab does not cancel a backend run or prove
the outcome of a physical action. Reopen the Agent page or run journal to
inspect retained state.

Both Agent pages expose **Stop task** while a run is active. It cancels the
selected run and the asynchronous subtasks owned by that run while preserving
Manager-controlled background Providers. Cancellation does not retract a
physical command that a controller has already accepted and does not prove its
outcome; inspect measured state and the terminal safe-state evidence before
starting another physical request.

### Use a bounded multi-Skill workflow

For a request containing a predetermined sequence of two or more eligible
finite Skills, the reference Agent should normally author one Limited Graph.
The graph should be visible as one top-level tool call, while its child calls,
Provider handovers, compact results, failures, and visual evidence remain
individually observable in the run events and journal.

Child visuals are streamed when each child produces them; they should not wait
for graph completion. Multiple visuals remain separate evidence records even
when the page layout shows only a subset at once. Use the developer page and
run journal to verify the originating run, graph, node, and child call.

Do not interpret graph transport completion as task or physical success.
Check terminal status, `last_completed_node`, compact `last_failure`, exhausted
limits, child outcome fields, physical completion, and required post-action
evidence. The current qualification boundary and known open tests are in
[Limited Graph Status and Qualification](14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md).

FoundationPose is available to the regular Agent only when the operator
explicitly mentions it by name. Matching is case-insensitive and tolerates
spacing, hyphenation, and minor spelling errors. A canonical request is:

`Use FoundationPose to establish the stationary world-to-arm-base transform.`

Generic alignment requests must not silently load it. The movement-based
six-degree-of-freedom replacement is still an active design; see
[Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md).

### Refine an existing arm-base alignment without movement

The discoverable
[`refine_arm_root_translation`](../skills/refine-arm-root-translation/SKILL.md)
Skill can improve XYZ translation after a trusted motion-usable alignment has
already established rotation. A typical Agent request is `Refine the arm
alignment with VLM.` The caller may add `using 5 samples` or an adaptation
factor from zero to one. Omitted sample count and adoption factor default to
one.

The operation itself never commands the robot. For the current bare-gripper
profile, every default call asks the VLM for both lateral endpoints of the
neon-green rail and averages their registered 3D points. The profile applies
the measured 80 mm rail-center-to-controller-tip offset along controlled-frame
+X before solving base translation. Rotation remains unchanged. Visual
evidence shows the exact RGB/depth inputs, selected points, derived rail
midpoint, and old/proposed base and selected-landmark projections.

Before using it, establish the current VIO world axis and an enforced
motion-usable world-to-arm-base alignment. The Skill also requires matching
camera calibration, VIO epoch, arm identity, and timestamp-bracketed FK across
each RGB-D capture window. Missing or extrapolated FK fails closed before VLM
correction and leaves alignment state unchanged. Repeated arm-FK/Fabric timing
failures are a Provider/Fabric investigation, not a reason to relax the
Skill's timestamp requirement; see the
[VIO and arm-FK timestamp anomaly handoff](../skills/refine-arm-root-translation/references/vio_and_arm_fk_timestamp_anomaly_handoff.md).

## Stop safely

Use **Shut down Midbrain** in the portal. When the browser is unavailable, run
`Stop Midbrain.cmd` or:

```powershell
.\platform_core\scripts\stop_workspace.ps1
```

The shutdown path orders safety-critical Providers, requests their defined safe
states, and requires acknowledgements. Do not close terminal windows or kill
processes as a substitute for safe arm shutdown. Independent emergency stop
remains outside this software path.

If Basic cannot reach its configured home position, the first shutdown leaves
the core available and reports the measured failure. Run the same shutdown
command again after inspecting the arm. On a repeated request, Basic may permit
termination when fresh advancing feedback proves all installed joints remained
stationary during the configured observation window; absolute joint position
is not a pass criterion. If Basic has already lost control, the repeated request
may release the failed process while explicitly reporting that the physical
outcome is unknown. Moving arms remain gated and continue through the bounded
safe-home attempt.

## Status and recovery

```powershell
.\platform_core\scripts\check_status.ps1
```

| Result | Response |
|---|---|
| Manager or Fabric unavailable | Run the bounded stop path, then start the workspace once. |
| Provider `COLD` | Request it through the portal or Agent only when needed. |
| Provider live but unready | Inspect its observation page and latest structured error. |
| Observation stale | Check the producing Provider, boot identity, dependency state, and source cadence. |
| Preview created | No movement has occurred; review the exact target, scene, limits, and evidence. |
| Completion unconfirmed | Treat the action as unsuccessful and inspect measured state before another request. |
| Development page unreachable | Use component observation and logs; process liveness does not guarantee a UI. |
| Arm endpoint reachable after Manager loss | Use the documented safety path; do not force-kill load-bearing control. |

Direct development endpoints are documented by Manager and each component.
The main stable local endpoints are Manager/portal `7001`, Fabric `7002`, and
Agent UI `8000`. Treat all of them as loopback-only development interfaces.

## Forced spatial reinitialization

Reinitializing space cognition creates a new VIO epoch. It revokes alignments
and observations bound to the previous epoch and requires stationary evidence.
It is not a routine readiness check. Use the non-resetting world-readiness path
when the existing epoch is valid.

Clearing the point-cloud display removes visualization state only; it does not
reset VIO or change the world frame.

## Next references

- [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md)
- [Validation](06_VALIDATION.md)
- [Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md)
- [reBot Basic safety](../providers/rebot_arm_dm/docs/SAFETY.md)
- [reBot Integrated safety](../providers/rebot_arm_integrated/docs/SAFETY.md)
- [reBot Contact Work safety](../providers/rebot_arm_contact/docs/SAFETY.md)
