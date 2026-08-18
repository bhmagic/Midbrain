# Physical AI Contracts

These documents define Midbrain's framework-neutral interoperability,
lifecycle, spatial, evidence, and physical-authority boundaries. They are
working contracts at different maturity levels; the version in one contract
is not the repository release version.

Machine-readable schemas under `contracts/schemas` take precedence over an
example payload in prose. A component should declare the exact contract and
schema versions it supports.

## Core Provider and Fabric contracts

| Contract | Scope | Maturity stated by document |
|---|---|---|
| [Terminology and Scope](00_terminology_and_scope.md) | Canonical names and boundary rules | v0.2 working draft |
| [Resource Provider Contract](01_resource_provider_contract.md) | Manifest, lifecycle, readiness, requests, observations, failures, fallback, and authority | v0.3.9 working draft |
| [Provider Implementation Guide](02_resource_provider_implementation_guide.md) | Recommended implementation structure | v0.3.9 guidance |
| [Fabric Transport Specification](03_world_state_fabric_transport_specification.md) | Observation envelopes, BufferRefs, pools, synchronization, and backpressure | v0.2 working draft |
| [Provider Conformance Tests](04_resource_provider_conformance_test_suite.md) | Core, transport, physical, GUI, and Skill integration tests | v0.3.9 working draft |
| [Safety and Lease Policy](05_safety_and_lease_policy.md) | Control authority, fencing, expiry, relinquish, and emergency-stop separation | v0.3.10 working draft |

## Spatial and Skill contracts

| Contract | Scope | Maturity stated by document |
|---|---|---|
| [Timestamped Transform Graph](06_timestamped_transform_graph.md) | Transform observations, composition, authority, and conflicts | v0.3 working draft |
| [Finite Skill Contract](07_skill_contract.md) | Bounded lifecycle, temporal policy, discovery, cleanup, and motion coordination | v0.3 working draft |
| [Device Calibration](08_device_calibration_contract.md) | Physical identity, persistence, ownership, and runtime bias separation | v0.3 working draft |
| [Local VIO and Space Cognition](09_local_vio_and_space_cognition.md) | Inertial-first Provider and initialization Skill | v0.4 working draft |
| [Motion Inhibit for Initialization](10_motion_inhibit_initialization_policy.md) | Stationary initialization coordination | v0.3 working draft |
| [Agent Skill Discovery](11_agent_skill_discovery.md) | Concise discovery, mandatory compact/detail result tiers, and adapter boundary | v0.3 mandatory contract |
| [Data-Route Advertisement](12_data_route_advertisement.md) | Direct payload-route discovery with Fabric-visible semantics | v0.1 working draft |
| [Component Observation UI](13_component_observation_ui.md) | Portal, observation, development transition, and UI descriptors | v0.1 advisory draft |
| [Spatial Frame Convention](14_spatial_frame_convention_v2.md) | +X forward, +Y left, +Z up semantics and native optical frames | v0.4 working draft |

## Agent, evidence, and history contracts

| Contract | Scope | Maturity stated by document |
|---|---|---|
| [Agent Event Stream](15_agent_event_stream.md) | SDK-neutral run, message, tool, approval, and replay events | v0.1 compatibility draft |
| [Visual Evidence and Annotations](16_visual_evidence_and_annotations.md) | Exact image channels, normalized annotations, and UI projection | v0.1 compatibility draft |
| [Agent Image Attachments](17_agent_image_attachments.md) | User-image upload and separation from robot evidence | v0.1 development contract |
| [Agent Chat History](18_agent_chat_history.md) | Manager-boot conversation projection and safe execution summary | v0.2 robot-local draft |
| [Agent Run Journal](19_agent_run_journal.md) | Durable normalized diagnostic events and read-only viewer | v0.2 local-diagnostics draft |
| [Robot Assembly and Free-Space Motion](20_robot_assembly_and_free_space_motion.md) | Assembly profiles, controller separation, and signed free-space goals | v0.1 working draft |
| [Contact Work Control](21_contact_work_control.md) | Independent contact Provider, finite Skill plans, wrench-to-joint effort limits, replacement, and relaxation | v0.1 working draft |
| [Limited Skill Graph](22_limited_skill_graph.md) | Immutable bounded composition, typed branching, retry, model routing, and child authorization carry | v0.1 development contract |

## How to use the set

- A new Provider starts with contracts 00–05 and adds the applicable spatial,
  calibration, route, or UI documents.
- A new Skill starts with contracts 07, 11, and the contracts for every
  observation or physical capability it consumes.
- A new Agent adapter starts with contracts 11 and 15–19.
- Any component that emits or consumes spatial values follows contracts 06 and
  14.
- Any physical controller follows contract 05 in addition to its hardware
  safety rules.

See [Compatibility and Extension](../docs/05_COMPATIBILITY_AND_EXTENSION.md)
for the implementation workflow.

## Open contract work

[Open Contract Items](OPEN_ITEMS.md) tracks unresolved normative questions.
Project delivery priorities remain in the
[Current Limitations and Roadmap](../docs/09_LIMITATIONS_AND_ROADMAP.md).

When a contract changes incompatibly, update its major version and the
corresponding schema identifier. Changelog entries and implementation notes do
not silently redefine a contract.
