# Canonical Terminology

These names should be used consistently in documentation, code, package names, logs, and discussion.

## Resource Provider Manager

The **Resource Provider Manager** is the control plane for Resource Providers. The short form is **Manager**.

It is responsible for provider discovery, lifecycle, health, dependencies, resource admission, provider selection, fallback, and physical-control authority. The current prototype implements only a small process-supervision subset of this vision.

Older shorthand such as `Capability Manager`, `resource runtime`, or `resource manager` should be replaced with **Resource Provider Manager** unless referring to historical text.

## World State Fabric

The **World State Fabric** is the state plane. The short form is **Fabric**.

It accepts timestamped observations from Resource Providers, distributes state metadata, and will eventually maintain canonical fused world state, transforms, temporal queries, freshness, and subscriptions.

Large payloads such as RGB frames, depth maps, point clouds, audio blocks, and tensors should remain in shared memory or another large-data transport. The Fabric carries access metadata such as a `BufferRef`, not the payload bytes.

Older shorthand such as `World State Service`, `state fabric`, or `state monitor` should be replaced with **World State Fabric** unless referring to historical text.

## Resource Provider

A **Resource Provider** is a persistent or reusable service that owns access to computation, data, hardware, models, recording, visualization, or control.

Examples:

- RGB-D camera provider
- Robot-arm hardware provider
- Hand-landmark provider
- Object-tracking provider
- ROS 2 transform bridge provider
- Recording or visualization provider

A Resource Provider may be `COLD`, `WARM`, or `HOT` and can serve multiple Skills over time.

## Skill

A **Skill** is finite, task-oriented orchestration. It begins for a purpose, may use one or more Resource Providers, returns a structured result, and finishes.

Examples:

- Identify the object a person is pointing at
- Estimate a grasp pose
- Move to a verified inspection pose
- Abort and Retreat

A Skill may internally use an agent framework, a state machine, LangGraph, conventional code, or a combination. A Skill is not a persistent hardware driver.

## Capability

A **capability** is a semantic function or data service offered by a Resource Provider and requested by a Skill or agent. A capability is not itself a process.

Examples include `camera.rgb`, `camera.depth`, `camera.rgbd_geometry`, `robot.arm.trajectory`, and `world.transform.query`.

## Agent Driver and Agent Adapter

The **Agent Driver** is the currently selected high-level reasoning and tool-selection loop. The prototype uses an OpenAI Agents SDK test driver.

An **Agent Adapter** is the narrow framework-neutral boundary between an agent framework and platform Skills. The long-term platform should not make the Manager, Fabric, Resource Provider contract, or safety model depend on one agent framework.

## Observation

An **Observation** is a provider-produced, timestamped statement about the physical or computational world. It includes provenance, schema, sequence, validity, freshness, and optional coordinate-frame or calibration information.

Providers publish observations. The Fabric decides how observations are accepted, expired, fused, or exposed as canonical state.

## BufferRef

A **BufferRef** is metadata that identifies a large payload stored outside the Fabric. It includes the transport, mapping or pool, slot, generation, offset, length, format, shape, and timing information needed to read and validate the payload.

A BufferRef is disposable. A consumer must expect a ring-buffer slot to be recycled and must reacquire a fresh reference when necessary.

## Residency and operation state

Provider residency modes are:

- `COLD`: no provider process exists.
- `WARM`: the process exists but releases protected control handles, high-rate work, and most optional resources.
- `HOT`: required initialization has completed and declared capabilities can become immediately usable.

`BUSY` is an operation state, not a residency mode. A provider can remain `HOT` while performing work.

Readiness should be capability-specific. A provider can be `HOT` while one capability is unavailable or degraded.

## Control Authority Lease

A **Control Authority Lease** is a Manager-issued, expiring, fenced authorization to command a protected physical resource.

A Skill may logically own the lease, but commands should normally pass through the hardware Resource Provider, which is the final software enforcement point. Release, expiry, revocation, or disconnection must trigger the provider's predefined safe-relinquish behavior.

## Native transform graph

The Fabric should own a framework-neutral, timestamped transform graph. ROS 2 `tf2` should remain available through a separate ROS 2 Transform Resource Provider that bridges `/tf` and `/tf_static` into the native graph.
