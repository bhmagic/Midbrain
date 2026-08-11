# Changelog

## Unreleased

- Treat reach/touch/until-reaching language as a no-contact boundary target,
  preserve the Integrated 0 mm WORK_OBJECT and 10 mm KEEP_OUT margins, and
  report closest-safe as graceful completion rather than refusal.
- Default the extra WORK_OBJECT standoff to 0 mm and leave observation
  uncertainty explicit instead of silently adding it as a second clearance
  margin. Integrated semantic collision geometry remains responsible for
  preventing contact and returning the closest-safe reachable point.
- Resolve the exact signed no-contact continuation through the autonomous
  free-space policy without a human motion-approval interruption.
- Keep the exact execution plan identifier entirely in current-turn host
  state. The Agent receives an argument-free continuation, so it cannot copy,
  select, or replay an older plan identifier.

- Make the default correction span the complete 1.2 m arm ROI, so an ordinary
  request plans to the no-contact destination instead of stopping after an
  arbitrary 5 cm diagnostic segment. Residual error is still measured by the
  mandatory post-move item/effector observation.
- Automatically reacquire transiently rejected item/effector evidence up to
  two times, and automatically rebuild one expired/stale/collision-changed
  controller preview from fresh observations before returning a blocker.
- The host execution adapter now binds Integrated `WAIT_FOR_NEXT`, allowing a
  subsequent freshly observed and revalidated correction to chain without an
  intermediate float or motor-mode transition. The bounded controller wait
  still expires to verified gravity float.

## 0.1.2

- Return a scene-compiler HOT activation and exact retry instead of terminating
  when the canonical semantic scene is unavailable.
- Add FREE_3D, NO_DESCENT, and same-height correction policies, and preserve
  controlled-frame orientation through per-plan POSE_6DOF IK.

## 0.1.1

- Bind a ready correction to the canonical scene, camera/VIO identities, and
  mounted workcell before requesting an Integrated shadow preview.
- Return the exact host execution continuation while keeping authorization and
  physical execution outside this read-only planning Skill.

## 0.1.0

- Compose the existing item and effector-front locators in one parallel observation cycle.
- Plan one uncertainty-aware, capped no-contact correction in `rebot_arm_base`.
- Require re-observation of both landmarks after every movement step.
- Keep controller preview, authorization, and physical execution outside this read-only Skill.
