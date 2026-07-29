# Changelog

## 0.1.0 - 2026-07-29

- Extracted review-only tool registration from the local cutting prototype.
- Added structured landmark observations, flexible-grid RGB-D registration,
  current robot tool-pose evidence, and exact camera/calibration/transform
  provenance.
- Added explicit nearby-valid-depth policies for reflective or thin tools
  whose visible tip or blade has no reliable depth.
- Returns a candidate only. It does not publish or activate a control frame,
  authorize movement, command a controller, or infer acceptance from a VLM
  response.
- Declares a generic shared-memory RGB-D route with an Orbbec-specific direct
  shared-memory fallback. Large image/depth payloads remain outside Fabric.
