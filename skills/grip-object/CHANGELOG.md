# Changelog

## 0.2.9 - 2026-08-21

- Start lower and scrap waits only after Contact reports trajectory completion;
  start the grip wait only after stable torque contact is inferred.
- Keep Contact's arm hold alive through the full close, grip dwell, settling,
  and carry confirmation interval.
- Add explicit Agent support for current-effector-relative approach entry. The
  Skill captures measured Integrated FK itself instead of using a VLM point as
  the current origin.

## 0.2.8 - 2026-08-21

- Add independent motion-profile waits after lower, scrap, and grip stages.
  New and migrated profiles default each wait to `1.5` seconds.
- Apply the same waits to regular Agent execution and attended development
  execution so the finite workflow does not submit all motions back-to-back.

## 0.2.7 - 2026-08-20

- Adopt the Grip Provider's owner-requested torque-only `0.15` N m stable
  Stage 4 contact policy and `85` C all-active-joint new-grip admission gate.

## 0.2.6 - 2026-08-20

- Move the owner-observed Stage 4 normal-object firm-grip target from `0` to
  `+12` degrees. Basic separately enforces `+17` degrees as the absolute close
  limit.
- Setup migrates only recognized earlier default close-target values;
  nonstandard close targets remain untouched.

## 0.2.5 - 2026-08-20

- Raised the owner-requested attended-development Stage 4 grip velocity from
  `0.7` to `4.0` rad/s and shortened the Stage 1 signed 50 Hz MIT opening
  request from `2.5` to `1.0` seconds.
- Retained the thermal gate, torque boundary, contact predicates, failed-grip
  release, and Basic measured-speed brake. The speed remains physically
  unqualified and exceeds the official reBot application `vlim` of `3.0` rad/s.

## 0.2.4 - 2026-08-20

- Move the owner-observed Stage 4 normal-object firm-close endpoint from
  `-10` to `0` degrees.
- Accept the Integrated managed endpoint completion slack increase used by
  Stage 1 alignment; collision, IK, authorization, velocity settling, and
  stable-sample requirements are unchanged.

## 0.2.3 - 2026-08-20

- Move the Stage 4 normal-object close endpoint from `-20` to `-10` degrees.
- When stable contact is not confirmed, open and float the gripper, relax
  Contact, and finish regular or attended execution with an explicit
  `FAILED_TO_GRIP` result. The developer page no longer leaves that expected
  no-object outcome as a pending failed session.
- Keep soft and thin object handling outside this Skill.

## 0.2.2 - 2026-08-20

- Declare the agent-facing `object_binding.object_id` field explicitly so the
  strict tool schema can accept a non-empty object binding.

## 0.2.1 - 2026-08-20

- Raise the default Stage 4 close request to the existing qualified 0.7 rad/s
  Provider cap and extend stable-contact inference from 3 to 10 seconds.

## 0.2.0 - 2026-08-20

- Stage 1 now opens the independently controlled gripper to the functional
  `-180` degree threshold while Integrated performs the rotation-only arm move.
- Stage 0 and Stage 1 both require fresh below-gate temperature feedback, and
  Stage 1 cannot complete before measured gripper approach readiness.
- Corrected the default grip target to the Integrated-compatible `-20` degree
  closed endpoint and added a safe migration for the installed default profile.

## 0.1.0 - 2026-08-19

- Added the finite two-vector scrap-grip workflow and attended four-stage UI.
