# Changelog

## 0.3.1-local - 2026-07-29

- Marked the vegetable-cutting prototype nondiscoverable and local-only while
  its calibration, VLM, RGB-D registration, tool registration, path planning,
  audit, and authorization responsibilities are separated.
- Kept the existing operator-supervised physical path for guarded development
  reference. This change does not claim autonomous slicing or make the legacy
  Skill eligible for agent selection.
- Moved general path-planning ownership toward the Integrated Controller,
  including singularity avoidance, speed selection, collision checks, route
  subdivision, authorization binding, execution audit, and endpoint hold.
- Kept slicing or axial cutting motion as a future independent Skill after the
  systematic cleanup and controller boundary are complete.
- Updated the development GUI to the common dark white/gray/black visual
  language. Semantic warnings and source images may retain color when color
  carries information.
- Kept the GUI as a development/debugging surface. Physical observation and
  authorization decisions remain explicit, decision-specific workflow
  boundaries rather than general manual control.

## 0.3.0

- Added the current supervised cutting prototype, bounded preview/commit
  workflow, stationary calibration dependency, fixed hard-mount tool frame,
  first-approach review, and guarded local development controls.
