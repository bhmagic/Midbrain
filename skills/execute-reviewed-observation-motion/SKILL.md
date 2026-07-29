---
name: execute-reviewed-observation-motion
description: Execute exactly one previously previewed and operator-approved Midbrain observation-motion decision. Use only when a decision-specific authorization record already exists, is still fresh, and the OpenAI Agents SDK must select the final physical commit skill without receiving coordinates, speeds, controller modes, or arbitrary motion authority.
---

# Execute Reviewed Observation Motion

Commit only the exact Integrated Controller preview named by one approved
authorization decision. Accept no motion parameters other than the decision ID.

## Procedure

1. Read the authorization record for the supplied decision ID.
2. Reject missing, pending, denied, expired, replayed, or non-observation
   decisions.
3. Require a controller preview authority bound to the controller instance,
   boot, configuration, request digest, preview digest, scene revision, and
   expiry.
4. Mint the one-time signed execution assertion through the authorization
   store.
5. Submit only the recorded preview identity and assertion to the Integrated
   Controller commit endpoint.
6. Return the controller result. Do not claim success unless the controller
   reports it.

Do not accept or infer Cartesian targets, joint targets, speeds, contact
permission, controller modes, lease changes, or fallback motions. Do not run
safe-home or gravity-float as an implicit success action. Controller errors
remain controller errors; the controller owns its configured fail-safe
behavior.

## Change log

- 0.1.1 (2026-07-29): Before minting the one-time execution assertion, the
  host adapter now restages the exact semantic scene stored with the reviewed
  decision and requires Integrated to accept the identical revision. The
  Agents SDK model still receives only the decision ID. This route completed
  one real no-contact 40-stage transit and left the controller holding the
  authorized endpoint.
- 0.1.0 (2026-07-29): Added the decision-ID-only Agents SDK execution boundary
  with host-side authorization, freshness, identity, digest, and one-time
  assertion enforcement.
