# Locate Arm Base Skill

Release history is recorded in [CHANGELOG.md](CHANGELOG.md).

`locate_arm_base` is one finite, non-moving Skill that establishes a reviewed
candidate for the world-to-arm-base transform. The active robot assembly
selects the arm-model profile, and that profile's namespaced flexible appendix
selects the base CAD, VLM reference images, first-VLM target guidance, semantic
origin, and bounded orientation candidates. The workflow remains
effector-independent.

The Skill owns validation and interpretation of its arm-profile appendix. CAD
and reference assets remain Skill-owned, while their exact paths and hashes are
selected by the active arm profile. Providers remain replaceable domain
functions.

## Finite workflow

1. Resolve the assembly-selected arm Provider, ask Manager for HOT residency,
   wait a bounded interval for its Provider-owned assembly state, and verify
   the active model/profile/appendix identity and digests.
2. Copy one current synchronized RGB/aligned-depth bundle from the camera
   Provider. For a full run, immediately bind `world_from_camera` at that exact
   capture timestamp and retain the active Local VIO frame and epoch. The
   developer-only visual diagnostic deliberately skips this world binding.
3. Independently compare the current RGB image with the Skill-owned base CAD
   atlas and full-arm no-effector reference through the configured number of
   VLM calls. Each call also receives the optional arm-specific
   `vlm_seed_guidance` stored in the selected profile. Each call returns one
   normalized positive seed point inside
   CAD-defined base geometry, one tight supporting box, and one negative point
   inside the external support below it. The default is two independent
   Gemini Robotics-ER 2.0 calls; touching pedestals, risers, enclosures, trays, and tables are
   explicitly excluded.
4. Request SAM2 HOT residency once, then send every independent prompt to a
   separate invocation of the Provider capability
   `perception.image.sam2.segment`; snapshot and verify every mask artifact.
   The generic Provider owns segmentation only and does not choose which
   robot-semantic mask is usable.
5. Render every independent SAM2 mask and ask one mask-review VLM to remove bad
   candidates. For every pixel, retain it when at least half of the surviving
   masks contain that pixel (`ceil(survivor_count / 2)`). Dilate that one voted
   mask exactly once using the configured final radius. The default mask-attempt
   count is two, but the developer UI may set it from 1–8 for one run.
6. Request FoundationPose HOT residency once, then reuse the single voted and
   once-dilated mask for an independently configurable number of pose requests
   containing the same CAD, RGB, depth, mask, and intrinsics. Render every
   returned pose as projected CAD over that shared mask and ask the VLM to
   select the best geometric fit. The native FoundationPose ranking value is
   retained for audit only and is neither shown to the VLM nor used as a
   selection threshold. Mask-attempt count and
   fit count are independent and each may be 1–8; every generated mask and every
   executed fit remains visible. Before VLM selection in a full run, apply one
   fixed local-X 180-degree half-turn to the known upside-down pose family, then
   exclude any fit whose semantic arm-base +Z still does not meet the configured
   world-up alignment. A low-confidence fit review is repeated; if two calls
   remain ambiguous, one final tie-break call is allowed. Acceptance requires
   either the normal confidence threshold or a unique candidate with at least
   two votes above the configured consensus floor.
7. Render one full-scene contact sheet for the profile's exact local-Z
   candidates: 0, 90, 180, and 270 degrees. Ask the VLM to select only one
   candidate by comparing the fixed base and first-arm geometry with the
   immutable references. Full-arm references may intentionally omit the
   interchangeable effector. The same bounded two-call-plus-one-tie-break
   consensus rule applies when orientation remains below the normal confidence
   threshold. Render a separate selected-pose overlay after the bounded
   world-up and local-Z correction, including the final semantic axes.
8. For a full run, query the captured timestamp again and require the Local VIO
   frame, epoch, and immutable historical transform to match the binding made
   before fitting. Then
   compose `world_from_camera @ camera_from_centered_mesh @
   orientation_correction @ centered_mesh_from_arm_base`.
9. Persist and publish an immutable `PENDING_REVIEW`, `motion_usable:false`
   candidate. Only Resource Provider Manager can validate review and activate
   `transform.world.arm_base`.

The Skill never observes or assumes a fixed gripper, tool, end-effector shape,
or FK relationship. VLM output cannot introduce an arbitrary quaternion or
translation: it can only select a candidate already defined by the model
profile.

## Setup and UI

```powershell
.\skills\locate_arm_base\scripts\setup.ps1
```

This creates a private Python 3.11 environment and runs the Skill tests. The
environment contains the Skill, lightweight HTTP/image dependencies, and the
provider-neutral BufferRef consumer only. It contains no FoundationPose,
SAM2, TensorRT, or Torch implementation. The Developer Agent invokes the Skill
with current camera evidence. The local developer surface exposes the selected
arm profile and editable appendix, the exact CAD filename sent to
FoundationPose, a static render bound to the exact CAD hash, every
reference/current/mask/candidate image supplied to a VLM or pose Provider,
the profile-backed first-VLM guidance, all mask overlays, the voted and
once-dilated masks, all projected-CAD fit renderings, the selected fit, the
resolved post-rotation pose and semantic axes, per-stage timings, a derived
depth preview, and the final result. The Developer Agent window receives every
mask and fit overlay plus the distinct resolved post-rotation pose as separate
visual-evidence cards, including structured failed runs. The default limited visual test runs
through independent VLM→SAM2 attempts, bad-mask review, pixel voting, one
dilation, repeated voted-mask fitting, and bounded orientation
in the camera frame without requiring a world axis or publishing a calibration
candidate. Separate mask-attempt and fit-count controls apply to one developer
 run and do not modify the robot arm profile:

```powershell
.\skills\locate_arm_base\scripts\run_ui.ps1
```

An Agent-owned run passes its selected visual model into the Skill explicitly.
The Skill derives the matching Gemini or OpenAI transport without importing the
Agent package. The Agent UI and standalone Skill both default to Gemini
Robotics-ER 2.0; selecting another visual model in the Agent UI applies that
same model to every VLM stage owned by this Skill for that run.

Saving from this explicitly guarded developer UI rewrites only
`appendix.midbrain.skill.locate_arm_base.v1` in the assembly-selected arm-model
profile and preserves unknown appendix fields. Asset hashes are recalculated.
The arm Provider must then restart and publish the matching assembly digest;
the Skill fails closed while the local and active profiles differ. The UI does
not own Provider startup, tracking, model installation, activation, motion, or
a second orchestration path.

The Developer Agent adapter establishes or verifies the current VIO world axis
before invoking the full Skill. Arm-provider readiness stays inside the Skill
because both the Agent and the standalone developer diagnostic require the
same profile-binding evidence. Manager remains the sole residency owner; the
Skill neither imports the arm Provider nor shares its environment. Every run
creates inspection evidence before readiness checks, including failed
preflight attempts.

## Model profiles

The central robot assembly selection already chooses an `arm_model`, in the
same way that it chooses a mounted-effector profile. The arm model may carry a
flexible `appendix`; the Skill consumes only the
`midbrain.skill.locate_arm_base.v1` namespace. To add a robot, add that appendix
to its Provider-owned arm profile and provide the referenced Skill assets.
Unknown appendix names and fields are preserved. Candidate rotation sets must
remain finite and explicit. Reference records may declare
`VLM_SEED_LOCALIZATION` and/or `VLM_ORIENTATION_SELECTION` consumers. A
full-arm semantic-axis image can be deliberately routed to both stages when it
helps distinguish the CAD-defined base joint from external support hardware.
The optional `vlm_seed_guidance` string is limited to 2000 characters and is
sent only to the VLM seed-localization stage.

## Safety and qualification

This Skill submits no physical motion. Its result cannot be consumed as a
motion transform until Manager verifies the immutable hash, signed review,
bounded orientation proof, VLM confidence, FoundationPose audit-only raw-ranking
provenance, timestamped world-axis proof, spatial conventions, and current
canonical camera identity and calibration revision.

Synthetic tests prove composition, hash immutability, fail-closed confidence,
independent VLM-prompt-to-SAM2 routing, VLM bad-mask rejection, at-least-half
pixel voting, one post-vote dilation, independent fit counts larger than mask
attempt counts, repeated use of the final mask, transient structured-output
recovery, stale-BufferRef recovery, bounded normalization of upside-down poses,
rejection of non-upright poses, qualified-majority tie breaking, epoch-scoped
world binding, and acceptance of finite negative raw ranking values.
Physical qualification still requires
ground-truth runs across the intended camera viewpoints, occlusions, lighting,
base finishes, and every supported robot profile.
