# Terminology and Scope

Version: 0.2 Working Draft

## Canonical names

- **Resource Provider Manager** or **Manager**: provider lifecycle, supervision, resource admission, selection, fallback, and physical-control authority.
- **World State Fabric** or **Fabric**: timestamped observations, state metadata, temporal queries, transforms, fusion, freshness, and subscriptions.
- **Resource Provider**: persistent or reusable service for hardware, data, computation, models, recording, visualization, or control.
- **Skill**: finite task-oriented orchestration that uses Resource Providers and returns a structured result.
- **Capability**: semantic function or data service offered by a Resource Provider; not a process.
- **Observation**: timestamped provider-produced state statement with provenance, schema, validity, and freshness.
- **BufferRef**: metadata for a large payload stored outside the Fabric.
- **Control Authority Lease**: expiring, fenced Manager authorization for a protected physical resource.

## Boundary rules

1. Resource Providers may be `COLD`, `WARM`, or `HOT`. `BUSY` is not residency.
2. Skills are finite and must not become hidden persistent hardware drivers.
3. Providers publish observations; they do not directly overwrite canonical state.
4. Large payload bytes should not traverse the Fabric when a suitable shared-memory or large-data transport is available.
5. BufferRefs are disposable and must be validated against slot generation and expiry.
6. Readiness should be capability-specific.
7. Unknown resource values are explicit and are never interpreted as zero.
8. The Fabric should own the framework-neutral transform graph; ROS 2 transforms enter through a Resource Provider.
9. Hardware Resource Providers enforce Control Authority Leases and safe relinquish.
10. Emergency stop remains independent of the Manager, Fabric, agent, Skills, and normal operating-system scheduling.

## Current document scope

This package defines the Resource Provider boundary, implementation guidance, Fabric transport semantics, conformance tests, and physical-control lease policy.

A complete formal Skill Contract, schema registry, transform specification, recording format, package-signing format, and remote-provider security contract remain open work.
