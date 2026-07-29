# Finite Skill Contract

Status: v0.3 working draft.

## Definition

A Skill is finite, task-oriented orchestration. It may activate persistent Resource Providers but does not become the long-lived owner of their sensor streams. Every run has a unique `skill_id` and produces structured status observations.

## Required lifecycle states

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `DEGRADED`
- `FAILED`
- `CANCELLED`

Status includes current subskill, selected providers, start/update timestamps, related command IDs, structured details, and a typed result when complete.

## Execution requirements

A Skill declares required and optional semantic capabilities, timeout, cancellation behavior, idempotency policy, provider residency behavior, cleanup, and reset semantics. When several capabilities belong to one physical Provider, activation is deduplicated by provider identity.

## Temporal responsibility

The Fabric remains a passive low-latency source of timestamped observations,
history, and temporal-association evidence. A Skill owns the decision that an
input is recent enough for its specific operation.

Every Skill that consumes observations declares:

- maximum source age where applicable;
- maximum inter-stream association error;
- required provider instance, boot, frame, calibration revision, and session
  epoch;
- allowed transform extrapolation;
- hard-invalid conditions such as recycled BufferRefs or revoked authority;
- post-compute continuity checks; and
- recovery outcomes such as `REOBSERVE`, `REBIND`, `RECALIBRATE`, or `REPLAN`.

A result derived from sensor data records the source observation timestamp,
Skill start and completion timestamps, source age at completion, input
identities, temporal-policy identifier, and the final temporal decision.
Result publication time never replaces the source observation time.

Long-running VLM or neural inference may finish after its input exceeds the
age accepted for a new capture. The Skill decides whether the historical
result remains useful, requires a cheap continuity check, or must be
recomputed. Fabric does not make that semantic decision.

## Depth-backed VLM landmarks

A general effector-front landmark uses the most distal point on the visible
rigid effector, mounted tool, or held tool that has valid registered depth.
Front is defined away from the wrist or arm, not by camera distance or image
direction.

The VLM reports integer coordinates in the original registered-depth grid.
The consumer validates the provider-declared valid region and requires valid
depth at each exact reported pixel. It may reject and re-observe but does not
silently snap the point to an unrelated neighboring surface.

A bare two-jaw gripper reports two distal points. Each is registered in 3D
before their mean is computed. Pixel averaging is not an accepted substitute.

This general landmark is not a task action point. Drill, hammer, blade, nozzle,
and other task-specific geometry use separate narrowly described Skills. A
landmark result grants no motion authority and publishes no control frame.

## Agent discovery

A Skill that may be selected automatically provides the concise
`agent_discovery` manifest metadata defined by
`11_agent_skill_discovery.md`. Agent selection is semantic and follows the
OpenAI Agents SDK function-tool name and description model. Provider-instance
selection is not part of the agent-visible Skill description.

During the advisory migration phase, the Skill requests capability bindings
from the Manager and may include explicit provider IDs only as fallbacks.
Existing direct provider-ID routes remain compatible until a later contract
revision explicitly enables binding enforcement.

## Resettable initialization Skills

A one-time initialization Skill may return an already-initialized result when a valid prior session exists. A forced reset creates a new session epoch, publishes a discontinuity, and never mutates old transform history.

## Motion coordination

A Skill that requires a stationary robot acquires a Manager motion-inhibit lease before measurement and releases it in all completion paths. The lease is a coordination contract today; future motion Providers must enforce the inhibit before accepting movement commands.
