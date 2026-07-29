# Changelog

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
