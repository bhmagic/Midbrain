# Midbrain integration

## Responsibility boundary

FoundationPose is a long-lived computational **Provider**. The Manager owns its
process lifecycle and residency. The Provider consumes timestamped sensor state
from the Fabric and publishes measurement observations back to the Fabric.

Task-specific interpretation belongs in Skills. In particular:

- Base/gripper orientation ambiguity is not silently corrected by the Provider.
- Visual measurements do not overwrite robot kinematics.
- Camera-to-robot calibration is not persisted by this Provider.
- Fusion of object pose, joint state, VIO/world state, or task constraints is
  external.

## Lifecycle

Registered Provider:

`perception.object_pose.foundation_pose`

Default registration uses `auto_start: false`.

Normal flow:

1. Manager starts the Provider.
2. Manager requests HOT residency.
3. A Skill/Agent sends `estimate` or `track` through the Manager.
4. The Provider reads the latest complete RGB-D bundle from Fabric.
5. The Provider publishes pose, transform, and status observations.
6. Manager stops or warms the Provider according to workload policy.

## Bounding-box initialization contract

The Provider advertises `perception.object_pose.bounding_box_init`. A Skill or
Agent may initialize FoundationPose by adding a rectangle to the normal Manager
request payload:

```json
{
  "action": "track",
  "payload": {
    "model_id": "robot_gripper_slider_support",
    "bounding_box": {
      "box_2d": [210, 480, 520, 720],
      "coordinate_space": "normalized_0_1000"
    }
  }
}
```

The rectangle order is `[ymin, xmin, ymax, xmax]`. The Provider validates and
rasterizes it into the same binary-mask type used by FoundationPose
registration. Initialization source precedence is local `mask_path`, request
bounding box, then the configured Fabric mask stream. Once initialization
succeeds, the same TRACK session uses `track_one()` and no longer consults the
rectangle unless `relocalize` supplies a new initializer.

## Fabric timestamp policy

The Provider extracts the source RGB BufferRef timestamp and uses it as
`observed_at_us` for pose and transform observations.

Inference completion time is retained as `latency_ms`; it is not substituted
for the acquisition timestamp.

## Transform authority

Published transform stream:

`transform.foundation_pose.object`

Schema:

`physical_agent.transform`

The edge is a visual measurement:

`femto_bolt_color_optical_frame -> observed_object/...`

Each dynamic edge contains:

- `authority`
- `session_epoch`
- `calibration_revision`
- covariance metadata
- `continuity: MEASUREMENT`
- source model/frame/mode metadata

Default reBot child frames are stable across sessions while `session_epoch`
separates tracking epochs:

- `observed_object/rebot_b601_dm/base`
- `observed_object/rebot_b601_dm/gripper_slider_support`

## Concurrency

The Provider can maintain multiple object sessions, but the NVLabs backend
serializes GPU inference behind one runtime lock. Base and Gripper sessions are
therefore time-sliced on the newest available RGB-D frame rather than executing
FoundationPose CUDA work concurrently.

This prevents unbounded GPU contention but means achieved update rate must be
measured on the deployed GPU.

## Shared memory

Large RGB-D payloads are not copied through Fabric JSON. The Provider reads
generation-checked camera BufferRefs through the existing Orbbec named
shared-memory access package.
