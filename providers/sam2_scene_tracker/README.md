# SAM2 Scene Tracker Provider

This HOT provider turns explicit user/upstream object descriptions into tracked
semantic scene geometry. It does not treat the full depth cloud as an obstacle,
and it has no checked-in bootstrap object policy. A user or upstream agent must
publish a non-empty description for every semantic object before obstacle
tracking can become ready. In particular, material, color, and location do not
silently make a black mat part of a table or any other blocking object.

The configured VLM model pool currently prefers Gemini Robotics-ER 2.0 and
falls back to the configured OpenAI vision model. Candidate model IDs are read
from `GEMINI_ROBOTICS_MODEL` and `OPENAI_VISION_MODEL`, with checked-in defaults,
so the available fastest model can change without modifying provider code.
Semantic annotations refresh
at 0.05 Hz during motion or low-confidence tracking and 0.025 Hz while stable.
One loaded SAM2.1 Base+ model tracks the user-described objects and complete
robot-arm mask at
1.25 Hz during motion, 0.75 Hz during acquisition or low confidence, and
0.25 Hz while stable. The arm mask is dilated before subtraction from every
declared object mask.

Before any new policy revision can produce spheres, a configured VLM reviews a
three-panel original-RGB, colored-mask, and registered-depth image. A rejected
mask restarts VLM annotation and SAM2 segmentation. After three rejected
annotation-segmentation-review attempts, the Provider publishes an explicit
invalid mapping result with zero assertions; geometry from the previous policy
revision cannot be reused. Tight VLM boxes and positive-point depth-connected
components additionally clip SAM2 spill onto the floor, arm, or nearby
workpieces before review.

Masked aligned depth is transformed into `rebot_arm_base` and fused into a
persistent 20 mm voxel map. Repeated observations merge into existing voxels;
occlusion retains prior cells. Camera identity, calibration, reviewed mounted
transform, policy revision, or target-frame changes reset the map. A Local VIO
process or epoch is not part of this mounted map identity. Published spheres use the
20 mm gripper ROI and 60 mm arm-base ROI policies.

Unclaimed visible depth is classified as `PUSHABLE` telemetry. It is not
published into controller collision geometry by default. The provider refuses
to advertise ready obstacle coverage until an explicit policy is received and
every described `KEEP_OUT` object has at least one fused voxel.

RGB and registered-depth views remain available even when the camera-to-arm
transform is unavailable. The exact RGB, registered depth, and reviewed
semantic mask overlay are available from `/v1/visualization/rgb.png`,
`/v1/visualization/depth.png`, and `/v1/visualization/composite.png` on the
Provider's local development endpoint. A successful user-requested scene
inspection registers these as switchable Agent visual evidence; the 3D viewer
shows the reduced sphere sample while the controller receives the complete
scene.

## Measured rate policy

The August 4, 2026 workcell benchmark used the live 1920 x 1080 aligned camera
frame, the pinned SAM2.1 Base+ checkpoint, and CUDA on this workstation. Seven
warm samples measured a 53.47 ms median image embedding, 37.01 ms for the table
and arm masks together, and 90.38 ms total. The slowest sample was 111.12 ms,
which gives approximately 9-11 Hz isolated compute capacity. The configured
0.25/0.75/1.25 Hz adaptive rates are one quarter of the earlier live policy.
They intentionally reduce camera, Fabric, GPU, and UI contention observed in
the integrated Agent workflow; isolated model capacity is not the limiting
factor. Keeping the model HOT still avoids a measured 2.17 second cold load and
preserves the prior-mask tracking prompt.

The recent agent journal contains 13 successful `locate_item` VLM tool samples
using `gemini-robotics-er-2-preview`. End-to-end time was 4.34 seconds median and
10.23 seconds at p90. For that reason the VLM does not run as a video-rate loop:
new label requests start every 20 seconds during motion or low SAM2 confidence,
and every 40 seconds while stable. The first annotation after activation remains
immediate. Only one request may be in flight, so slow responses are coalesced
instead of queued against old frames.

## Documentation

- [Changelog](CHANGELOG.md) — release history; not an operating procedure.
