# Validation

Version 0.1.0 is software-tested development code and has not been physically
qualified.

`scripts/verify.ps1` compiles the independent package, runs its unit tests, and
parses its configuration templates. Repository validation also runs the shared
finite Skill runtime tests and validates all Contact Skill manifests and
contract JSON.

Current unit coverage includes:

- exact-plan HMAC assertion binding and mismatch rejection;
- signed Manager-to-Basic authority lineage shadow comparison;
- `HOT` admission and `WARM` rejection without a retained Basic lease;
- independent FK, locked-joint weighted 6-DoF IK with best-iterate retention,
  and additive force-plus-torque `J^T w`;
- full Basic-authorized N·m torque ceiling on explicitly locked joints;
- immediate new-sequence endpoint replacement;
- retention of a signed session across a pre-first-setpoint feedback failure,
  with fresh feedback still required by the first move;
- Basic lease servicing during deliberately slowed multi-knot Cartesian IK
  construction;
- Basic-declared per-joint velocity-limit consumption and transition timing
  returned for Skill-side minimum command spacing;
- Basic-rate discovery and a Contact-owned Cartesian segment that produces
  changing joint targets at the advertised 50 Hz cadence through sequential
  IK knots no farther than 2 mm apart;
- acceptance of finite best-effort unreachable targets without an arrival
  success claim;
- non-zero rotational wrench acceptance at the Provider boundary;
- explicit relaxation to Basic `IMPEDANCE` and lease release;
- priority-preserving slicing orientation, three-step engage/slice/retract
  planning, zero rotational torque, and Integrated-before-Contact ordering; and
- Skill terminal relax and Manager authority release after normal, failed, and
  transport-ambiguous session submission.

No motor was commanded during this software validation. Physical validation
must not use a low-torque or low-stiffness load-bearing state. The next physical
phase requires the user's six-degree-of-freedom torque boundary, guarded
workcell preparation, current blade development-v3 measurements, Basic fault
and float observation, and separate physical qualification of slicing and each
later task Skill.
