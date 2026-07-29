# Effector-Front Landmark Boundary

`locate-effector-front` is the general visual landmark Skill for the distal
front of the current rigid effector assembly. The assembly may be the bare
effector, a mounted tool, or a firmly held tool.

## Meaning of front

Front means most distal along the visible rigid assembly away from the wrist
or arm. It does not mean closest to the camera, lowest in the image, or the
task-specific point that performs work.

The selected point must have valid registered-depth evidence. If the nominal
tip is reflective, thin, or sharp and has no depth, selection retreats only as
far as needed along the same rigid assembly to the most distal valid-depth
pixel. The result may therefore identify a tool body or handle rather than the
visible nominal tip.

For a bare two-jaw gripper with two distinct distal fronts, the VLM must return
both points. Each point is independently deprojected and transformed into the
target frame. Control math uses the mean of those two registered 3D points; it
does not average their image pixels.

## RGB-D evidence and coordinates

The VLM receives one lossless three-panel image:

- RGB resampled onto the registered-depth grid;
- registered depth with invalid samples clearly marked; and
- RGB dimmed where registered depth is invalid.

The runtime evidence preserves native registered-depth pixels. Structured
coordinates are integer `[y, x]` indices in the original registered-depth grid,
including its provider-declared valid boundary. The local resolver requires
valid depth at the exact reported pixel. It records neighborhood support but
does not silently snap an invalid VLM point to a nearby foreground or
background sample.

The Skill rejects ambiguous assemblies, background or work-object selections,
an invalid exact depth sample, a point outside the registered-depth valid
region, and an incomplete two-jaw result.

## Result boundary

The result preserves capture, provider instance and boot, route, alignment,
calibration, transform, VIO epoch, VLM backend, source timestamp, completion
time, and source-age evidence. Camera binding is revalidated after VLM
inference. The Skill, not Fabric, applies the completion-age policy.

The result is read-only. It may be eligible as geometric input to later control
math, but it is not motion authorization, is not a motion-usable command, and
does not publish a control frame.

## Separation from action geometry

Drill tips, hammer faces, cutting edges, dispensing nozzles, and similar
task-specific action points require separate narrowly prompted Skills. The
existing `register-tool-to-control-frame` Skill is also separate: it constructs
a review-only 6-DoF task frame from three specialized landmarks. It is not the
general effector-front locator.

The new Skill is present in semantic discovery and has a Test Agent execution
adapter. It is deliberately absent from the active Agent tool allowlist until
recorded and live nonphysical VLM evidence is reviewed.
