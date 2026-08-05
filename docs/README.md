# Documentation Index

This directory is the canonical reading order for the GitHub repository.

The 2026-08-05 Test Agent v0.4.9 checkpoint is summarized in the canonical [Architecture and Data Flow](01_ARCHITECTURE_AND_DATA_FLOW.md), [Setup and Operation](03_SETUP_AND_OPERATION.md), [Main GUI Portal](04_MAIN_GUI_PORTAL.md), [Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md), [Version History](11_VERSION_HISTORY_AND_DECISIONS.md), and [Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md). It retains the single autonomous regular/developer Agent runtime and adds Fabric-regulated semantic scenes, no-contact approach composition, full-destination POS_SPEED motion, before/after arm-root evidence, and an explicit-only FoundationPose boundary. The framework-neutral Agent boundaries remain specified in [Agent Event Stream](../contracts/15_agent_event_stream.md), [Visual Evidence](../contracts/16_visual_evidence_and_annotations.md), [Image Attachments](../contracts/17_agent_image_attachments.md), [Chat History](../contracts/18_agent_chat_history.md), and [Run Journal](../contracts/19_agent_run_journal.md).

The earlier 2026-07-29 guarded-motion evidence remains available in [Phase 5 Agent SDK Completion and Shutdown](reference/project_notes/PHASE5_AGENT_SDK_COMPLETION_AND_SHUTDOWN_20260729.md), [Agent SDK Roadblocks](reference/project_notes/OPENAI_AGENT_SDK_ROADBLOCKS_20260729.md), [Component Changes](reference/project_notes/COMPONENT_CHANGES_20260729.md), and [Cartesian Axis and Alignment Open Issue](reference/project_notes/CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md).

| Order | Document | Purpose |
|---|---|---|
| 1 | [Overview](00_OVERVIEW.md) | Scope, current baseline, safety boundary, and working capabilities. |
| 2 | [Architecture and Data Flow](01_ARCHITECTURE_AND_DATA_FLOW.md) | Manager, Fabric, Providers, Skills, BufferRefs, transforms, startup, runtime, and reset flow. |
| 3 | [VIO Sensor-Fusion Design](02_VIO_SENSOR_FUSION_DESIGN.md) | Inertial-first ESKF design and visual correction policy. |
| 4 | [Setup and Operation](03_SETUP_AND_OPERATION.md) | Windows prerequisites, installation, command-line fallback, and recovery. |
| 5 | [Midbrain Main GUI Portal](04_MAIN_GUI_PORTAL.md) | Canonical operator entry point for observation, activation, Agents, developer escalation, and shutdown. |
| 6 | [Validation](06_VALIDATION.md) | Automated checks, hardware checks, and acceptance criteria. |
| 7 | [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md) | Local configuration, secrets, calibration ownership, Agent API/record trust boundaries, and publish exclusions. |
| 8 | [Workspace Audit](08_WORKSPACE_AUDIT.md) | Undocumented differences found between the handover snapshot and working workspace. |
| 9 | [Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md) | Known technical gaps and next milestones. |
| 10 | [Release and GitHub](10_RELEASE_AND_GITHUB.md) | Clean build, staged-file review, upload script, and release checklist. |
| 11 | [Version History and Decisions](11_VERSION_HISTORY_AND_DECISIONS.md) | Milestone history and decisions that should be preserved. |
| 12 | [FoundationPose Object Pose](12_FOUNDATIONPOSE_OBJECT_POSE.md) | Base/Gripper initialization, mask refinement, Fabric transforms, and camera-alignment boundary. |
| 13 | [Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md) | Next-iteration plan for movement-based rigid registration, close-range translation refinement, and the explicit-only FoundationPose boundary. |

## Reference material

Detailed framework contracts remain under [`contracts`](../contracts). Component-specific documentation remains beside each component:

- [`platform_core/docs`](../platform_core/docs)
- [`providers/orbbec_femto_bolt/docs`](../providers/orbbec_femto_bolt/docs)
- [`providers/foundation_pose/docs`](../providers/foundation_pose/docs)
- [`providers/rebot_arm_dm`](../providers/rebot_arm_dm)
- [`providers/rebot_arm_integrated/docs`](../providers/rebot_arm_integrated/docs)
- [`skills/stationary_world_arm_alignment`](../skills/stationary_world_arm_alignment/README.md)
- [`test_agent/docs`](../test_agent/docs)
- [Next gripper-alignment iteration handoff](reference/project_notes/NEXT_ITERATION_GRIPPER_ALIGNMENT_HANDOFF_20260805.md)

Earlier component-first tutorials are retained under
[`docs/archive`](archive/README.md). Earlier planning, research, and handover
notes remain under
[`docs/reference/project_notes`](reference/project_notes). Both locations are
historical references; the numbered documents above are the canonical
operational documentation.
