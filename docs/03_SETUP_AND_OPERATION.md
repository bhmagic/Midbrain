# Setup and Operation

## Target environment

The complete hardware path is designed for Windows 10/11 with Developer PowerShell for Visual Studio 2022.

Required tools and hardware:

- Visual Studio 2022 Build Tools with C++ workload
- Rust stable MSVC toolchain with `cargo`, `rustfmt`, and `clippy`
- Python 3.11
- CMake
- Orbbec SDK 2.8.6 development files
- Orbbec Femto Bolt

Optional FoundationPose operation additionally requires:

- NVIDIA CUDA-capable hardware and a compatible CUDA/PyTorch environment
- The upstream NVLabs FoundationPose runtime
- Git LFS for the two published checkpoint files
- SAM2 and an OpenAI API key only when using the assisted GUI initialization path

Optional reBot arm operation additionally requires:

- The supported reBot/Damiao seven-motor assembly and its reviewed local calibration
- A Windows serial connection and `motorbridge>=0.4.9`
- An Xbox-compatible controller only for the manual Integrated hardware-test GUI

The Orbbec SDK is not redistributed in this repository.

## Workspace location

The scripts support other paths, but the established workspace is:

`C:\Projects\testing_physical_ai`

Do not place API keys or device calibration in tracked source files. The setup scripts create or preserve the local `config` directory.

## First setup

Open Developer PowerShell:

```powershell
cd C:\Projects\Midbrain_git_migration
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup sequence:

1. Builds the Rust Manager and Fabric in release mode.
2. Creates `providers/orbbec_femto_bolt/.venv` and installs the Orbbec support package.
3. Builds CameraHost unless `-SkipCameraBuild` is supplied.
4. Creates `providers/local_vio/.venv` and installs the Local VIO Provider.
5. Creates a private `.venv` for each Python Skill in the core workspace.
6. Creates `test_agent/.venv` for the Test Agent and OpenAI Agents SDK.
7. Creates missing local configuration from examples without overwriting existing machine-local files.

There is no repository-root Python environment. A component may install local
editable dependencies into its own environment, but launch scripts always use
the environment owned by the process they start.

The workspace setup does not install FoundationPose or build the upstream
NVLabs CUDA runtime. Install its assets/backend, then set up the Stationary
Alignment parent. The finite Skill route is the default:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\skills\stationary_world_arm_alignment\scripts\setup.ps1
```

The Provider setup preserves the compatibility runtime and seeds model assets.
The Stationary setup installs the finite FoundationPose Skill runtime into the
parent Skill environment. Neither command compiles the complete upstream
FoundationPose runtime.

Set up the two reBot arm Providers independently so each owns its own `.venv`:

```powershell
.\providers\rebot_arm_dm\scripts\setup.ps1 -WithMotorBridge
.\providers\rebot_arm_integrated\scripts\setup.ps1
.\providers\rebot_arm_dm\scripts\register.ps1
.\providers\rebot_arm_integrated\scripts\register.ps1
```

The setup commands create the two private environments and seed missing active configuration from the checked-in clean templates. The registration commands add or update the two entries in local `config\providers.json`. The repository includes factory/unverified arm templates, but it does not include an operator's active measured calibration, active controller tuning, or either virtual environment.

To set explicit SDK paths:

```powershell
.\platform_core\scripts\setup_workspace.ps1 `
  -OrbbecIncludeDir "C:\Program Files\OrbbecSDK 2.8.6\include" `
  -OrbbecLibrary "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib" `
  -OrbbecBinDir "C:\Program Files\OrbbecSDK 2.8.6\bin"
```

## Start and enter Midbrain

For normal Windows operation, double-click `Start Midbrain.cmd` from the
workspace root. It starts Manager, Fabric, and the idle Agent UI service, then
opens the main Midbrain portal at `http://127.0.0.1:7001/`.

The portal is the primary interaction surface. Opening it does not activate a
Provider, execute a Skill, or authorize physical motion. Providers remain
`COLD` until an operator or approved Agent workflow requests them.

Use the PowerShell launcher for automation, recovery, or development:

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Options:

- `-NoBrowser`: start without opening the portal.
- `-StartAgentUi`: start the regular and developer views on port 8000.
- `-AllowProviderAutoStart`: explicitly honor Provider `auto_start` entries.
- `-CoreOnly`: compatibility alias for Manager + Fabric only; it cannot be
  combined with `-StartAgentUi`.

`Start Midbrain.cmd` supplies `-StartAgentUi`. The direct PowerShell default
does not. Neither path honors Provider auto-start without the explicit flag.

## Operate from the main portal

Use the portal in this order:

1. Confirm the Manager and Fabric summaries are live.
2. Read Provider and Skill cards before starting anything. Process liveness,
   residency, readiness, data freshness, and active work are separate signals.
3. Open a component's read-only observation page for its manifest,
   capabilities, streams, latest state, and recent error details.
4. Enter a component development UI only through its guarded link. Acknowledge
   that administrative controls can overstep the Agent.
5. If the component is stopped, review the activation request and confirm only
   when the hardware and work area are ready.
6. Open the regular Agent for ordinary tasks. Use its developer view when the
   same run needs additional Provider, Skill, replay, or event diagnostics.
7. Use **Shut down Midbrain** when finished. It invokes
   `platform_core\scripts\stop_workspace.ps1` through the guarded portal flow.

The portal links have distinct authority:

| Portal link | Purpose and side effects |
|---|---|
| Provider/Skill observation | Read-only state and diagnostics; does not activate or execute. |
| Development UI | Requires overstepping acknowledgement; may request bounded activation before opening. |
| Regular Agent | Curated typed Skills and bounded session-policy authorization. |
| Developer view | Same autonomous Agent behavior with additional read-only diagnostics. |
| Agent Run Journal | Read-only retained normalized events; no run or robot authority. |
| Shut down Midbrain | Runs the safety-ordered workspace stop after confirmation. |

See [Midbrain Main GUI Portal](04_MAIN_GUI_PORTAL.md) for the complete operator
workflow and recovery guidance.

## Agent interaction from the portal

The single Agent, from either browser view, may inspect current runtime state and propose Provider
lifecycle changes. An operation outside the active session policy presents a
plain-language development confirmation naming the Provider, requested state,
and hardware consequence.

For supported relative arm motion, the Agent should:

1. Inspect current runtime state, even if an earlier conversation reported the
   controllers running.
2. Request policy authorization for Basic to reach `HOT`.
3. Request policy authorization for Integrated to reach `HOT`.
4. Produce a nonphysical IK preview from the latest measured pose.
5. Evaluate session policy for execution of that exact preview and present a
   development approval only when the policy does not resolve it.
6. Wait for the bounded controller result and report success only when physical
   completion is confirmed.

Repeated relative commands are cumulative from the latest measured pose. The
Agent does not convert them into absolute world-coordinate requests. Safe-home
is a separate policy-gated Basic Controller operation.

Both Agent pages support per-run Agent model, reasoning-effort, and configured
visual-backend selection. Terra with medium reasoning is the balanced default.
A stronger model can improve interpretation but cannot replace controller
validation, approval, fencing, collision checks, or physical safety controls.
Both prompt panels start the same backend-owned autonomous run path and observe
it through the same replayable SSE contract. The sole execution contract is
`POST /api/streaming-runs` plus its status, SSE-events, and decision routes.
The synchronous `/api/run` path and `/api/dev/...` execution aliases are not
available.

Each run appears as a separate user/Agent turn in a bounded scrollable history
owned by the current Manager boot session.
Expand **Execution summary** on a turn to inspect public model reasoning-summary
text plus safe Agent, tool, approval, and retry lifecycle updates. Raw private
reasoning and tool payloads are not shown. The regular and developer pages read
the same robot-local SQLite projection and periodically synchronize, so both
show the same transcript when open together and a reopened tab restores the
same Manager-boot session. There is no browser clear-history action.

The prompt panels can attach one JPEG, PNG, or WebP image up to 8 MiB. Selection
creates only a browser-local preview; starting a run uploads and validates the
image, then sends its bounded Midbrain attachment ID in the run request. The
selected Agent model receives the prompt and image together. Uploaded images
are not robot observations and do not carry capture time, calibration, depth,
spatial-frame, or physical-action authority. Robotics-ER Skills continue to
capture the live bot camera independently.

Visual inference automatically retries a classified transient failure on the
same read-only backend before using the next configured backend. The default
is two attempts per backend with a 0.25-second backoff, controlled by
`PHASE4_VLM_ATTEMPTS_PER_BACKEND` and `PHASE4_VLM_RETRY_BACKOFF_S`.
Authentication, invalid-model, and other non-transient failures are not
repeated. This policy does not retry a complete Agent task or robot action.

For a visual Skill that produces annotations, the Agent page shows the exact
retained camera image used for inference. Overlay visibility and color are
browser controls. Multiple annotations receive distinct colors and expose
independent labeled swatches; **Reset colors** restores the deterministic
palette. **Copy annotated** and **Download annotated** create a flattened PNG
with those colors without changing the source evidence. SVG and flattened
exports use the same compact medium-weight labels and translucent black halo.
Channel buttons appear
only for channels supplied by that evidence record. The present pointing and
scene Skills publish RGB only; do not interpret the absence of a depth button
as a camera-depth failure.

After a cold dependency requests `HOT`, the Agent lifecycle tool waits up to 20
seconds for a fresh Manager report showing that the Provider is `HOT` and
ready. `HOT` is the correct dependency action even when the process is stopped,
because Manager starts it as part of the transition. If the model instead
chooses `START` with an exact `required_capability`, that call uses the same
wait rather than returning at process creation. The lifecycle tool also waits
for the capability to become available from the same Provider. Only then does
the model resume the original finite Skill. This interval is configured by
`PROVIDER_HOT_READINESS_TIMEOUT_S`.

A plain `START` with a null capability remains process-only. If `START` with a
required capability times out because a Provider remains `WARM`, the result
supplies an exact `HOT` continuation instead of telling the model to retry the
finite Skill against an unready dependency.

The visual capture boundary then independently waits up to 12 seconds for the
first readable RGB `BufferRef`, configured by
`CAMERA_FIRST_FRAME_TIMEOUT_S`. The second check covers the narrow data-plane
race after control-plane readiness and recycled shared-memory slots. If that
bounded capture attempt times out, the finite visual Skill retries only the
RGB capture by default, controlled by `CAMERA_SKILL_CAPTURE_ATTEMPTS` (`1..3`)
and `CAMERA_SKILL_RETRY_BACKOFF_S` (`0..5`). Camera binding is retained, VLM
inference waits for a usable frame, and no physical action is submitted. A
camera that exhausts those bounded attempts fails visibly rather than waiting
indefinitely or requiring a second user request.

## Direct endpoints for development and recovery

Use these when the portal is unavailable or when developing a component:

| Service | URL |
|---|---|
| Midbrain main portal | `http://127.0.0.1:7001/` |
| Manager health/control | `http://127.0.0.1:7001/v1` |
| Fabric health/state | `http://127.0.0.1:7002` |
| Camera Provider control | `http://127.0.0.1:7101` |
| Local VIO Provider control | `http://127.0.0.1:7102` |
| FoundationPose Provider control | `http://127.0.0.1:7103` |
| reBot Arm DM Basic control | `http://127.0.0.1:8791` |
| reBot Arm Integrated control/GUI | `http://127.0.0.1:8793` |
| Regular Agent UI | `http://127.0.0.1:8000/` |
| Autonomous Agent developer view | `http://127.0.0.1:8000/dev` |
| Agent Run Journal | `http://127.0.0.1:8000/dev/run-journal` |
| Calibration GUI | `http://127.0.0.1:8111` |

## Status and stop

```powershell
.\platform_core\scripts\check_status.ps1
.\platform_core\scripts\stop_workspace.ps1
```

Runtime logs are written under `platform_core\logs` and are intentionally ignored by Git.

## reBot arm discovery and test operation

When Integrated is HOT and ready, Manager `GET /v1/capabilities` advertises usable MIT one-shot/continuous and limited POS_VEL one-shot. Provider `GET http://127.0.0.1:8793/v1/capabilities` maps the discoverable capabilities and GUI operations to their HTTP or Fabric invocation.

POS_VEL one-shot is labeled limited to paths at or below 20 cm with no payload or high external load. POS_VEL continuous and arm POS_TOR one-shot remain experimental/unstable GUI tests and are intentionally absent from Manager capability discovery.

The Agent flow stages target/settings through Fabric stream
`robot_arm.primary.integrated.command`, binds authorization to one exact
unexpired preview, and then requests the Integrated one-shot commit. Manager
authority, Basic fencing, Integrated limits, and terminal completion evidence
remain enforced. The separate manual hardware-test GUI retains its documented
Engage + Xbox LB development release. Use the Provider's documented
`stop_physical_gui_test.ps1` path for authoritative termination of that manual
GUI session.

## Forced VIO reinitialization

Use **Force reinitialize origin** in the Test Agent while the camera is stable. A reset creates a new Local VIO session epoch and world frame. The viewer suspends new insertion, switches epoch, clears old-epoch points, reopens shared-memory readers, and resumes capture when the new pose and RGB-D data are available.

Expected progression:

`SUSPENDED_FOR_REINITIALIZATION → WAITING_FOR_NEW_SESSION_FRAME → CAPTURING`

## Clear visualization only

Use **Clear point cloud** to remove accumulated display points without resetting VIO or changing the current coordinate epoch.

## FoundationPose compatibility operator workflow

The normal Stationary Alignment workflow invokes FoundationPose as a bounded
Skill and releases its backend automatically. Use the legacy tracking GUI only
for compatibility diagnostics or guarded route comparison:

```powershell
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

Keep the arm still during initialization. Freeze a suitable RGB-D frame, request and review the Base and Gripper boxes and positive points, generate the cropped SAM2 masks, inspect the refined results, and submit tracking only when both masks cover the intended rigid surfaces without unrelated geometry.

The tested Base refinement uses median Lab color distance 30 followed by radius-2 dilation. The tested neon-green Gripper-root refinement uses a median RGB seed with 10% per-channel drift followed by radius-2 dilation. These are empirical defaults, not universal segmentation guarantees.

Base tracking is selectable up to 10 Hz. The experimental Gripper selector exposes rates up to 60 Hz, but actual throughput remains bounded by inference and hardware load; raising the requested rate did not correct the observed Gripper stability problem. Use the lowest stable rate that supplies timely measurements.

After a compatibility job, stop every owned session and send
`release_resources`, or transition the Provider to `WARM`. Both paths unload
the backend; the request is rejected while any session is still active.
