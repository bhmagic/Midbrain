# Changelog

## 0.2.3 - 2026-08-03

- Keep the selected registered-depth point as the no-contact control surface,
  but estimate scene/viewer geometry behind it at the projected box center.
- Replace the oversized half-diagonal sphere with half the shorter projected
  box dimension, suitable as a representative cross-section sphere.

## 0.2.2 - 2026-08-03

- Standardize VLM point and box geometry on explicit normalized 0-1000 image
  coordinates, then convert once to the registered-depth grid in deterministic
  host code. Version 1 native-pixel payloads remain accepted for compatibility.
- Preserve both source and converted geometry plus the conversion policy for
  audit and replay.

## 0.2.1 - 2026-08-03

- Publish a successful metric item result as a short-lived canonical Fabric
  `WORKPIECE` assertion for the HOT arm scene compiler.
- Preserve the metric location and expose structured degraded evidence when
  semantic assertion publication fails.

## 0.2.0 - 2026-08-03

- Replaced the disabled future workflow with a discoverable read-only item
  locator that reuses synchronized spatial registration.
- Added trustworthy registered-depth, bounded same-surface neighbor,
  task-plane intersection, and bearing-only outcomes.
- Prevented reflective, transparent, and thin geometry from silently using
  background depth.
- Kept controller preview, authorization, and physical motion outside the
  locator contract.

## 0.1.0 - 2026-07-29

- Defined the future agent-facing workflow for identifying a pointed object,
  registering it in the stationary workcell, asking Integrated for a front or
  top observation-path preview, and requesting permission only at the
  physical-motion boundary.
- Defined approval as a separate record that does not itself execute motion.
- Marked the Skill nondiscoverable because structured pointing-pixel output and
  the complete nonphysical adapter are not implemented.
- Kept all physical execution outside this Skill. The separately discoverable
  `execute-reviewed-observation-motion` Skill may commit only an exact,
  separately approved decision.
