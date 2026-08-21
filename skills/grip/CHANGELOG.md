# Changelog

## 0.1.9 - 2026-08-21

- Size the Contact current-pose hold watchdog from the configured contact
  inference timeout plus settling margin, so a slow close cannot expire the
  arm hold before carry confirmation.

## 0.1.8 - 2026-08-20

- Adopt the Grip Provider's owner-requested torque-only `0.15` N m stable
  contact policy and `85` C all-active-joint new-grip admission gate.

## 0.1.7 - 2026-08-20

- Move the owner-observed normal-object firm-grip target from `0` to `+12`
  degrees. Basic separately enforces `+17` degrees as the absolute close limit.
- Setup migrates only recognized earlier default close-target values;
  nonstandard close targets remain untouched.

## 0.1.6 - 2026-08-20

- Raised the owner-requested attended-development firm-grip velocity from
  `0.7` to `4.0` rad/s. Basic retains its `0.75` native FORCE_POS translation,
  measured-speed brake, torque boundary, thermal gate, and contact predicates.

## 0.1.5 - 2026-08-20

- Move the owner-observed normal-object firm-close endpoint from `-10` to `0`
  degrees.
- Before Contact locks the current pose, require Integrated to be in verified
  FLOAT, transition it to WARM, verify that its Basic arm-group lease is
  released, and then activate Contact. This prevents `grip` from colliding
  with an idle Integrated lease.

## 0.1.4 - 2026-08-20

- Move the default normal-object close endpoint from `-20` to `-10` degrees.
- Treat missing stable contact as a terminal unsuccessful task result: verify
  functional opening, float the gripper, relax Contact, and return an explicit
  `FAILED_TO_GRIP` message instead of propagating only a timeout.
- Keep soft and thin object handling outside this Skill.

## 0.1.3 - 2026-08-20

- Declare the agent-facing `object_binding.object_id` field explicitly so the
  strict tool schema can accept a non-empty object binding.

## 0.1.2 - 2026-08-20

- Raise the default close request to the existing qualified 0.7 rad/s Provider
  cap and extend stable-contact inference from 3 to 10 seconds.

## 0.1.1 - 2026-08-20

- Corrected the default current-pose grip target to the Integrated-compatible
  `-20` degree closed endpoint and added a safe installed-default migration.

## 0.1.0 - 2026-08-19

- Added the generic current-pose grip and carry-confirmation Skill.
