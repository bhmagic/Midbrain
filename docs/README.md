# Documentation Index

This directory is the canonical reading order for the GitHub repository.

The 2026-07-29 guarded-agent checkpoint is summarized in the canonical [Validation](06_VALIDATION.md), [Workspace Audit](08_WORKSPACE_AUDIT.md), [Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md), and [Version History](11_VERSION_HISTORY_AND_DECISIONS.md) documents. Detailed evidence is retained in [Phase 5 Agent SDK Completion and Shutdown](reference/project_notes/PHASE5_AGENT_SDK_COMPLETION_AND_SHUTDOWN_20260729.md), [Agent SDK Roadblocks](reference/project_notes/OPENAI_AGENT_SDK_ROADBLOCKS_20260729.md), [Component Changes](reference/project_notes/COMPONENT_CHANGES_20260729.md), and [Cartesian Axis and Alignment Open Issue](reference/project_notes/CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md).

| Order | Document | Purpose |
|---|---|---|
| 1 | [Overview](00_OVERVIEW.md) | Scope, current baseline, safety boundary, and working capabilities. |
| 2 | [Architecture and Data Flow](01_ARCHITECTURE_AND_DATA_FLOW.md) | Manager, Fabric, Providers, Skills, BufferRefs, transforms, startup, runtime, and reset flow. |
| 3 | [VIO Sensor-Fusion Design](02_VIO_SENSOR_FUSION_DESIGN.md) | Inertial-first ESKF design and visual correction policy. |
| 4 | [Setup and Operation](03_SETUP_AND_OPERATION.md) | Windows prerequisites, setup, start/stop, endpoints, and normal operation. |
| 5 | [Point Cloud and Pose Tutorial](04_TUTORIAL_POINT_CLOUD_AND_POSE.md) | Mock-agent example and functional check. |
| 6 | [IMU Calibration Tutorial](05_TUTORIAL_IMU_CALIBRATION.md) | Six-position accelerometer calibration GUI example and functional check. |
| 7 | [Validation](06_VALIDATION.md) | Automated checks, hardware checks, and acceptance criteria. |
| 8 | [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md) | Local configuration, secrets, calibration ownership, and publish exclusions. |
| 9 | [Workspace Audit](08_WORKSPACE_AUDIT.md) | Undocumented differences found between the handover snapshot and working workspace. |
| 10 | [Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md) | Known technical gaps and next milestones. |
| 11 | [Release and GitHub](10_RELEASE_AND_GITHUB.md) | Clean build, staged-file review, upload script, and release checklist. |
| 12 | [Version History and Decisions](11_VERSION_HISTORY_AND_DECISIONS.md) | Milestone history and decisions that should be preserved. |
| 13 | [FoundationPose Object Pose](12_FOUNDATIONPOSE_OBJECT_POSE.md) | Base/Gripper initialization, mask refinement, Fabric transforms, and camera-alignment boundary. |

## Reference material

Detailed framework contracts remain under [`contracts`](../contracts). Component-specific documentation remains beside each component:

- [`platform_core/docs`](../platform_core/docs)
- [`providers/orbbec_femto_bolt/docs`](../providers/orbbec_femto_bolt/docs)
- [`providers/foundation_pose/docs`](../providers/foundation_pose/docs)
- [`providers/rebot_arm_dm`](../providers/rebot_arm_dm)
- [`providers/rebot_arm_integrated/docs`](../providers/rebot_arm_integrated/docs)
- [`skills/stationary_world_arm_alignment`](../skills/stationary_world_arm_alignment/README.md)
- [`test_agent/docs`](../test_agent/docs)

Earlier planning, research, and handover notes are retained under [`docs/reference/project_notes`](reference/project_notes). They are historical references; the numbered documents above are the canonical operational documentation.
