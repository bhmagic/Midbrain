# Midbrain Main GUI Portal

The Midbrain main GUI at `http://127.0.0.1:7001/` is the primary interaction
portal for normal operation. Start here instead of memorizing component ports
or launching individual development GUIs.

The portal is observation-first. Opening it does not activate hardware,
execute a Skill, or authorize motion. It shows the system that is available,
what is currently running, whether data is fresh, and which guarded action can
be taken next.

## Enter Midbrain

For normal Windows use, double-click `Start Midbrain.cmd` in the repository
root. The launcher starts:

- Resource Provider Manager
- World State Fabric
- The idle regular and developer Agent UI service
- The browser at the Midbrain portal

Providers remain stopped until an operator or an approved Agent workflow
requests them. This remains true even if an older machine-local registry
contains `auto_start: true`.

If the browser was closed, reopen `http://127.0.0.1:7001/`. Do not launch the
Manager a second time.

## Read the system before acting

The top-level view is the system summary. Check Manager and Fabric first. They
must be live before Provider, Skill, or Agent workflows can be trusted.

Provider and Skill cards separate several concepts that should not be treated
as synonyms:

| Signal | Meaning |
|---|---|
| Process liveness | Whether the component process currently exists. |
| Residency | Whether a Provider is `COLD`, `WARM`, or `HOT`. |
| Health/readiness | Whether the component reports that it can perform its declared work. |
| Data freshness | Whether recent observations are arriving within their expected age. |
| Active work | Whether a Skill or Provider currently owns an active operation. |

A live process can still be unready, unhealthy, or publishing stale data. A
`HOT` Provider can also have an optional capability unavailable. Use the
component observation page before escalating to administrative controls.

## Observe a Provider or Skill

Select the observation link on a Provider or Skill card. Observation pages are
read-only and information-rich. Depending on the component, they show:

- Manifest identity and declared capabilities
- Process, residency, health, and readiness
- Published streams and the latest structured observations
- Freshness, timestamps, instance/boot identity, and recent errors
- Current robot/controller state when the component supplies it
- A guarded link to the component's development UI, when one exists

Not every finite Skill needs a separate observation implementation. The portal
still presents its manifest and known lifecycle state when possible.

## Enter a development UI

Development UIs expose administrative controls and may overstep the Agent's
normal authority. The portal therefore uses an explicit transition:

1. Open the component observation page.
2. Select its development UI link.
3. Read the warning and acknowledge that the UI can overstep Agent control.
4. If the component is stopped, review the activation request.
5. Confirm only when the hardware and work area are ready.

For a cold Provider, confirmation requests the required lifecycle transition
and waits for a bounded readiness result before opening the development UI. A
finite Skill's development link may start its UI host, but it does not execute
the Skill itself.

If activation succeeds but the development page is not reachable, return to
the component observation page. The component may be live without hosting a
development page at the expected address. Inspect its reported state and log
path instead of repeatedly activating it.

## Use the regular Agent

Open **Regular Agent** from the main portal for normal natural-language tasks.
The regular Agent can:

- Inspect the current Provider and Skill catalog
- Request approval to activate required Providers
- Invoke its curated typed Skills
- Request approval to establish a new spatial origin when explicitly asked
- Preview supported relative arm motion
- Request approval for the exact physical-motion preview
- Request the Basic Controller safe-home operation

For arm motion, the intended flow is:

1. Inspect current runtime state.
2. Request approval to activate Basic to `HOT`, if needed.
3. Request approval to activate Integrated to `HOT`, if needed.
4. Create a nonphysical IK preview from the latest measured pose.
5. Present a plain-language approval for that exact preview.
6. Execute only after approval and report the controller's bounded completion
   result.

Each repeated relative request is another displacement from the latest
measured pose. The Agent does not silently reinterpret it as an absolute
world-coordinate request.

`reinitialize_space_cognition` is a deliberate epoch transition, not a
readiness check. Its approval warns that Midbrain will revoke active
stationary-workcell calibration, reset Local VIO, clear observations bound to
the old epoch, and require any later world-to-arm calibration to be established
again. Keep the robot and camera stationary for the operation.

The Agent page allows per-run model, reasoning-effort, and configured visual
backend selection. Terra with medium reasoning is the balanced default. Model
quality may improve planning and interpretation, but it never replaces
Provider validation, approval, fencing, collision checking, or physical safety
controls.

## Use the developer Agent

Open **Developer Agent** when testing discovery, routing, or a wider typed tool
catalog. It can inspect more Provider and Skill adapters and can prompt across
the developer-visible surface.

Developer mode is not an approval bypass. Provider lifecycle changes,
safe-home, and physical execution retain their separate human confirmations.
Use the regular Agent for ordinary operation and the developer Agent when the
extra visibility is part of the test.

FoundationPose is normally invoked as a finite nested Skill by Stationary
Alignment. Its backend resources are released before the parent returns. If
the compatibility Provider is used for diagnostics, stop every session and
request `release_resources`, transition it to `WARM`, or let the bounded parent
stop it when no foreign sessions remain.

## Shut down Midbrain

Use **Shut down Midbrain** in the main portal. Review the shutdown warning and
confirm once. The action invokes
`platform_core\scripts\stop_workspace.ps1`, which performs the bounded,
safety-ordered workspace shutdown.

`Stop Midbrain.cmd` is the desktop fallback when the browser is unavailable.
Do not close terminal windows as a substitute for safe shutdown of an active
arm Provider.

## Recover from common portal states

| Portal result | Operator response |
|---|---|
| Manager or Fabric is unavailable | Use `Stop Midbrain.cmd`, then start once. Do not launch duplicate workspaces. |
| Provider is `COLD` | Activate it only through the guarded portal or Agent flow when needed. |
| Provider is live but unready | Read its observation details and latest error; do not repeatedly issue the same activation. |
| Data is stale | Verify the producing Provider and upstream dependencies before trusting the observation. |
| Agent reports a dependency unavailable | Let it inspect current runtime and request the required Provider activations. |
| Agent reaches a nonphysical preview | Review the generated execution approval; no movement has occurred yet. |
| Physical completion is unconfirmed | Treat the move as unsuccessful and inspect measured state before issuing another request. |
| Development UI cannot be reached | Use the observation page and component logs; liveness does not guarantee a development server. |

## Command-line fallback

The portal is the normal interaction surface. Direct scripts and component
URLs remain available for setup, CI, recovery, and development. They are
documented in [Setup and Operation](03_SETUP_AND_OPERATION.md) and beside each
component. When a portal path and an old tutorial disagree, the portal guide
and current component manifest take precedence.
