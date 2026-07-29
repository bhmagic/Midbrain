# Phase 5 Authority Lineage Decision

Date: 2026-07-28

Status: shadow architecture corrected; enforcement not enabled.

## Decision

Authority coordination has three layers:

1. Manager task authority identifies the upstream owner, scope, lifetime, and
   Manager fencing generation.
2. Integrated active action context identifies the exact request, preview,
   authorization, upstream owner, and Manager authority lease used by the
   current operational writer.
3. Basic operational lease identifies Integrated as the hardware Provider
   client and uses Basic's independent fencing generation.

Manager and Basic lease generations are separate namespaces. They must never be
compared or copied. The exact Manager lease must instead be bound through the
Integrated active action context.

## Why direct lease-to-lease binding is wrong

Integrated acquires and renews the Basic lease as part of HOT residency. It may
hold that lease while gravity-floating, editing a target, previewing a path, or
otherwise standing by with no task writer. Releasing and reacquiring Basic for
every Manager task would couple task scheduling to hardware mode and support
transitions, increasing the risk of short gaps, drops, and lease/mode races.

Therefore:

- a held Basic lease does not mean an upstream operational writer is active;
- a Manager lease may exist while Integrated remains in local standby;
- idle Manager-plus-Basic lease coexistence is not a disagreement;
- exact upstream lineage is required only when an operational writer becomes
  active; and
- Basic remains the final local fence and gravity-support boundary even if
  Manager, Fabric, browser, Agent SDK, or the upstream client becomes
  unavailable.

## Current implementation

The versioned `physical_agent.authority_coordination_state` evaluator reports:

- Manager task-authority state;
- Basic lease-held state;
- Integrated operational-writer state;
- separate Manager and Basic fencing namespaces;
- controller residency and control state;
- motion inhibit, authorization, and relinquishment context;
- exact upstream owner and Manager lease lineage when supplied;
- stable state and disagreement codes; and
- poll, transition, state, and disagreement counts.

The evaluator is `SHADOW_OBSERVE`. It cannot acquire or replace a lease, switch
mode, submit a motor command, or weaken Basic fencing. HOT idle is classified
as `LOCAL_LEASE_STANDBY` or `MANAGER_PRESENT_LOCAL_STANDBY`, not as a writer
conflict.

The current active action boundary does not yet carry a verified Manager owner
and authority lease ID. Consequently, a Manager-authorized active writer is
reported as `DUAL_LAYER_UNCORRELATED` with
`AUTHORITY_LINEAGE_NOT_BOUND`. An opaque audit `authority_id` is not sufficient
proof, and the signed transit assertion currently binds UI authorization rather
than Manager authority.

## Steady implementation route

1. Define a versioned Integrated active-action-context record containing the
   Manager resource, owner, lease ID, Manager fencing generation, permissions,
   issue/expiry, request/preview/authorization digests, and controller
   instance/boot.
2. Validate that record with a bounded direct Manager query at preview and
   again immediately before any commit. Fabric may receive a later copy but is
   not the synchronous authority source.
3. Bind the exact record digest into the controller preview and signed UI
   assertion. Do not accept a caller-supplied opaque ID as proof.
4. Activate the context only for the exact operational writer and clear it on
   completion, explicit release, cancellation, error-to-float, lease loss,
   preemption, or shutdown.
5. Keep the Basic lease and its fencing namespace unchanged across ordinary
   upstream task changes. Transfer task ownership through the action context,
   not through hardware lease churn.
6. First enforce a non-motion admission transition that rejects creation of a
   physical commit authority when the Manager context is absent, stale,
   mismatched, expired, or preempted. Do not change a motor mode in that first
   enforcement trial.
7. Rerun replay and shadow failure injection before any guarded handoff test.

## Required failure coverage before enforcement

- Manager lease expiry, renewal loss, release, and preemption;
- wrong owner, lease ID, resource, permission, or Manager generation;
- stale action context after preview or authorization;
- duplicate commit and late command delivery;
- Basic lease loss or Basic restart while Manager authority remains;
- Integrated restart while Manager and Basic state persist;
- Manager or Fabric loss while Basic retains gravity support;
- browser/client disconnect and Agent SDK cancellation/idle timeout;
- motion inhibit during standby and during an active writer;
- explicit release, error-to-float, safe-home, and partial shutdown; and
- proof that no path creates a second operational writer.

VLM landmark semantics and the compound pointed-object loop are not part of
this authority decision. The general effector-front landmark was later defined
separately; specialized action points and the compound loop remain on hold.
