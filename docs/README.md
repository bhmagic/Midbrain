# Midbrain Documentation Hub

This hub indexes the active documentation. It is organized by what a reader is
trying to do rather than by the order in which documents were created.

## Choose a path

### Understand the platform

1. Read the project [aim and infrastructure rationale](../README.md#aim-autonomous-robots-without-framework-lock-in).
2. Read [Architecture and Data Flow](01_ARCHITECTURE_AND_DATA_FLOW.md).
3. Use the [contract index](../contracts/README.md) when implementing or
   reviewing an interoperability boundary.

### Operate the reference robot

1. Read [Setup and Operation](03_SETUP_AND_OPERATION.md).
2. Review [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md).
3. Run the checks in [Validation](06_VALIDATION.md).
4. Consult the relevant Provider's `README.md`, `SAFETY.md`, and
   `VALIDATION.md` before activating physical hardware.

### Add another Provider, Skill, or Agent

1. Start with [Compatibility and Extension](05_COMPATIBILITY_AND_EXTENSION.md).
2. Select the applicable documents in the [contract index](../contracts/README.md).
3. Follow [Contributing](../CONTRIBUTING.md) and keep implementation-specific
   instructions beside the component.

### Continue current engineering work

- [Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md) contains
  only active gaps and priorities.
- [Runtime catalog and Skill result-tier feasibility](performance/2026-08-17-runtime-and-skill-result-tier-feasibility.md)
  records the measurements, approved design, and implemented compact Manager
  catalog plus mandatory two-tier Skill-result checkpoint.
- [Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md)
  records the implemented non-moving translation refiner and the remaining
  automatic multi-movement six-degree-of-freedom alignment work.
- [Release and GitHub](10_RELEASE_AND_GITHUB.md) covers local validation and
  publication mechanics.
- [Changelog](../CHANGELOG.md) is the release and milestone history.

## Active framework documentation

| Document | Authority and scope |
|---|---|
| [Architecture and Data Flow](01_ARCHITECTURE_AND_DATA_FLOW.md) | Explanatory system architecture. Contracts and runtime schemas take precedence. |
| [Setup and Operation](03_SETUP_AND_OPERATION.md) | Current workspace setup, portal operation, recovery, and shutdown. |
| [Compatibility and Extension](05_COMPATIBILITY_AND_EXTENSION.md) | Practical entry point for outside Providers, Skills, and Agent adapters. |
| [Validation](06_VALIDATION.md) | Current validation commands, evidence classes, and remaining qualification. |
| [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md) | Local configuration ownership, secrets, trust boundaries, and publication exclusions. |
| [Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md) | Active limitations and prioritized work only. |
| [Release and GitHub](10_RELEASE_AND_GITHUB.md) | Maintainer release workflow. |
| [Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md) | Mixed implementation record: the non-moving XYZ refiner is available; the automatic multi-movement six-degree-of-freedom workflow remains active design. |

The VIO implementation design belongs to
[`providers/local_vio`](../providers/local_vio/README.md). FoundationPose setup
and compatibility details belong to
[`providers/foundation_pose`](../providers/foundation_pose/README.md) and the
finite
[`foundation_pose_object_localization`](../skills/foundation_pose_object_localization/README.md)
Skill. Component-specific controller and camera details remain beside those
components.

## Documentation source-of-truth rules

When two statements conflict, use this order:

1. Machine-readable schemas, manifests, and enforced runtime validation.
2. Versioned contracts under `contracts`.
3. Component README, safety, API, and validation documentation beside the
   implementation.
4. Framework and operator guides under `docs`.
5. Roadmaps and active design plans.
6. Changelogs and Git history for historical behavior.

Mutable facts such as package versions, test counts, model defaults, ports,
and capability maturity should have one owner. Other documents should link to
that owner rather than copying the value.

## Document lifecycle

Active documents describe current behavior or an explicitly active design.
Completed phase plans, temporary handovers, superseded progress snapshots, and
one-time workspace audits are retired from the active tree. Their commits
remain available through Git history. Release-relevant outcomes are retained
in the root or component changelog, while current validation boundaries remain
in `VALIDATION.md` files.

An active design must say what is implemented, what is proposed, what can
grant physical authority, and what constitutes acceptance. A completed design
is folded into the owning contract and component documentation, then retired.

## Contracts and component references

- [Contracts](../contracts/README.md)
- [Platform Core](../platform_core/README.md)
- [Orbbec Femto Bolt Provider](../providers/orbbec_femto_bolt/README.md)
- [Local VIO Provider](../providers/local_vio/README.md)
- [SAM2 Scene Tracker](../providers/sam2_scene_tracker/README.md)
- [Arm Scene Compiler](../providers/arm_scene_compiler/README.md)
- [reBot Arm Basic Provider](../providers/rebot_arm_dm/README.md)
- [reBot Arm Integrated Provider](../providers/rebot_arm_integrated/README.md)
- [reBot Arm Contact Work Provider](../providers/rebot_arm_contact/README.md)
- [Contact Work Skill Authoring](../providers/rebot_arm_contact/docs/CONTACT_SKILL_AUTHORING.md)
- [FoundationPose Compatibility Provider](../providers/foundation_pose/README.md)
- [Stationary World-Space Arm Alignment Skill](../skills/stationary_world_arm_alignment/README.md)
- [Refine Arm-Root Translation Skill](../skills/refine-arm-root-translation/SKILL.md)
- [Reference Agent](../test_agent/README.md)
