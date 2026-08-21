# SAM2 Scene Tracker Provider

This HOT provider turns explicit user/upstream object descriptions into tracked
semantic scene geometry. It does not treat the full depth cloud as an obstacle,
and it has no checked-in bootstrap object policy. A user or upstream agent must
publish a non-empty description for every semantic object before obstacle
tracking can become ready. In particular, material, color, and location do not
silently make a black mat part of a table or any other blocking object.

The same resident model exposes the generic
`perception.image.sam2.segment` capability for bounded one-shot segmentation.
A calling Skill supplies immutable RGB evidence, a normalized box, one to four
positive seed points, and optional negative background points; the Provider
returns a scored mask artifact and source identity. It does not call a VLM,
choose an object, own CAD/reference images,
or decide how the mask will be used. One-shot requests share the loaded model
under the Provider's inference lock; the regular scene loop resets its current
image on the next tick and retains its own temporal policy state.

The SAM2 runtime remains in `providers/sam2_scene_tracker/.venv`. Its only
shared camera-data dependency is the provider-neutral BufferRef consumer under
`contracts/python`; it does not install FoundationPose, the camera Provider,
or the calling Skill.

The configured VLM model pool currently prefers Gemini Robotics-ER 2.0 and
falls back to the configured OpenAI vision model. Candidate model IDs are read
from `GEMINI_ROBOTICS_MODEL` and `OPENAI_VISION_MODEL`, with checked-in defaults,
so the available fastest model can change without modifying provider code.
Semantic annotations refresh
at 0.05 Hz during motion or low-confidence tracking and 0.025 Hz while stable.
One loaded SAM2.1 Base+ model tracks the user-described objects and complete
robot-arm mask at the fixed `tracking_rate_hz`. The supported range is 1-4 Hz
and the default is 1 Hz. The arm mask is dilated before subtraction from every
declared object mask. Arm and visual motion still control the slower VLM
annotation refresh; they do not silently change the configured SAM2 rate.

Before any new policy revision can produce spheres, a configured VLM reviews a
three-panel original-RGB, colored-mask, and registered-depth image. A rejected
mask restarts VLM annotation and SAM2 segmentation. After three rejected
annotation-segmentation-review attempts, the Provider publishes an explicit
invalid mapping result with zero assertions; geometry from the previous policy
revision cannot be reused. Tight VLM boxes and positive-point depth-connected
components additionally clip SAM2 spill onto the floor, arm, or nearby
workpieces before review.

After review and semantic precedence, geometry masks receive a configurable
registered-depth erosion before any 3D projection. The default boundary margin
is 10 mm for `WORK_OBJECT` and 20 mm for `KEEP_OUT`. The Provider measures one
mean depth for the complete object mask and converts the metric margin into one
constant pixel radius using the camera focal length. That same pixel erosion is
applied around the complete boundary. The eroded geometry mask is the single
input to semantic voxel fusion, hand-centric collision spheres, and work-object
AABBs; the review overlay remains the unmodified SAM2 evidence image.

If the current camera-to-`rebot_arm_base` transform is unavailable, the
Provider continues reviewed 2D mask tracking but publishes an explicit invalid
current-policy mapping result with a structured external prerequisite. It does
not silently reuse an older 3D scene.

Masked aligned depth is transformed into `rebot_arm_base`. A persistent 20 mm
voxel map remains the coverage and reacquisition record: repeated observations
merge into existing voxels and occlusion retains prior cells. Camera identity,
calibration, reviewed mounted transform, policy revision, or target-frame
changes reset that map. A Local VIO process or epoch is not part of this
mounted map identity.

Controller collision output is a separate, bounded projection of the latest
accepted masked-depth frame. The origin is the current controlled-hand center.
The `SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1` profile provides 4,096 near-uniform
directions over 4 pi steradians without latitude/longitude polar crowding.
Every visible semantic depth point maps to its exact nearest Fibonacci
direction using a constant-size four-candidate inverse lookup; only the nearest
hit in each occupied direction is retained. Therefore the global semantic
output is sparse and never exceeds the configured direction count. Sphere
radius grows with hit range and the configured angular covering ratio, with a
5 mm close-range floor and 3 mm radial padding. A `KEEP_OUT` sphere is shifted
away from the hand so its near boundary remains tangent to the measured
surface. Shared profile, exact hand-origin, angle, and radius-policy metadata
is published once in `angular_projection` instead of being repeated on every
sphere. Sphere geometry is rounded to one micrometre, well below the collision
policy scale, to reduce Fabric serialization and retention cost. The compiler
and controller continue to consume ordinary spheres.

Each currently visible `WORK_OBJECT` also publishes a
`VISIBLE_SURFACE_AABB` aligned to `rebot_arm_base`, with its own observation
timestamp and a default five-second expiry. Named corners follow the canonical
arm axes: forward `+X`, right `-Y`, and up `+Z`; for example,
`right_forward_up` is `[xmax, ymin, zmax]`. This bound is an agent-planning
reference over the visible surface, not a solid-object tracker or controller
collision primitive. `KEEP_OUT` obstacles remain sphere-only and receive no
AABB or per-object text label from this path.

Unclaimed visible depth is classified as `PUSHABLE` telemetry. It is not
published into controller collision geometry by default. The Provider requires
an explicit policy, but that policy may contain no `KEEP_OUT` object. Readiness
then describes a valid scene with zero blocking objects. When `KEEP_OUT`
objects are explicitly described, each must still produce usable SAM2 depth
geometry before obstacle coverage is advertised. SAM2 output is accepted
without a second VLM mask-quality call; missing or empty required masks remain
a segmentation failure.

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
which gives approximately 9-11 Hz isolated compute capacity. The fixed rate is
configurable from 1-4 Hz and defaults to 1 Hz to reduce camera, Fabric, GPU, and
UI contention in the integrated Agent workflow. Isolated model capacity is not
the limiting factor. Keeping the model HOT still avoids a measured 2.17 second
cold load and preserves the prior-mask tracking prompt.

The recent agent journal contains 13 successful `locate_item` VLM tool samples
using `gemini-robotics-er-2-preview`. End-to-end time was 4.34 seconds median and
10.23 seconds at p90. For that reason the VLM does not run as a video-rate loop:
new label requests start every 20 seconds during motion or low SAM2 confidence,
and every 40 seconds while stable. The first annotation after activation remains
immediate. Only one request may be in flight, so slow responses are coalesced
instead of queued against old frames.

## Documentation

- [Changelog](CHANGELOG.md) — release history; not an operating procedure.
