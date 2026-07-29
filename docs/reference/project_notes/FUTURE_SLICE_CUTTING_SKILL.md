# Future Slice-Cutting Skill

Status: deferred until systemic housecleaning and guarded control handover pass

The future slicing Skill will own food-specific behavior: selecting slice
locations, axial/sawing motion semantics, cut progress, re-observation between
cuts, and task-level recovery. It will not own camera transport, RGB-D
registration, tool registration, generic VLM routing, singularity avoidance,
speed limits, collision checking, or controller trajectory generation.

Its dependencies will be:

- a valid `calibrate-stationary-workcell` revision;
- a registered tool control frame with explicit acting point and axes;
- a VLM/geometry result for board and workpiece;
- controller-owned path previews and time parameterization;
- decision-specific authorization at physical safety boundaries.

The initial behavior should use a bounded axial slicing component rather than
the current pure downward push. The controller should receive the geometric
intent and produce a continuous, speed-limited, singularity-aware trajectory.
The Skill should observe outcomes and decide whether another slice is needed;
it should not stream joint commands or implement its own interpolator.

No slicing implementation or physical test is part of Phase 2.
