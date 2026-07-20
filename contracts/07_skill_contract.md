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

## Resettable initialization Skills

A one-time initialization Skill may return an already-initialized result when a valid prior session exists. A forced reset creates a new session epoch, publishes a discontinuity, and never mutates old transform history.

## Motion coordination

A Skill that requires a stationary robot acquires a Manager motion-inhibit lease before measurement and releases it in all completion paths. The lease is a coordination contract today; future motion Providers must enforce the inhibit before accepting movement commands.
