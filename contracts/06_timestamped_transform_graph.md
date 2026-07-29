# Native Timestamped Transform Graph Contract

Status: v0.3 working draft.

## Purpose

The World State Fabric owns a framework-neutral graph of rigid transforms. Providers publish transform observations; consumers query composed transforms at a requested timestamp. Calibration transforms and estimated motion transforms use the same schema but retain different authority, validity, and revision semantics.

## Transform observation

Schema: `physical_agent.transform`, version 1.

Required data fields:

- `parent_frame` and `child_frame`.
- `translation_m`: three finite metres.
- `rotation_xyzw`: normalized quaternion in XYZW order.
- `is_static`: boolean; false denotes a dynamic observation.
- `authority`: provider identity responsible for the edge.
- `session_epoch`: required for resettable estimated frames and optional for static calibration.
- `calibration_revision`: required when the edge derives from calibration.
- `covariance`: optional 6x6 pose covariance, with declared tangent convention.
- `max_extrapolation_us`: maximum permitted forward or backward extrapolation for a dynamic edge.

The observation timestamp is the transform timestamp. Receipt time is not a substitute for acquisition time.

## Graph eligibility

Fabric always retains an accepted transform observation in its ordinary latest
and recent stream history for inspection. That does not imply that the
observation is eligible to participate in the composed transform graph.

A transform is excluded from the graph and transform catalog when:

- its observation has `valid=false`;
- its observation-level `expires_at_us` is in the past;
- its data explicitly has `motion_usable=false`; or
- its data has `review_state=CANDIDATE_REVIEW_REQUIRED`.

When `review_state=CANDIDATE_REVIEW_REQUIRED` is present,
`motion_usable=false` is mandatory. This keeps calibration candidates visible
without allowing a stream-name change, static-edge behavior, or direct Fabric
query to bypass review. Legacy transforms without review fields remain
graph-eligible for compatibility until their producing subsystem is migrated.

## Query

`GET /v1/transform` accepts `from_frame`, `to_frame`, optional `at_us`, optional `session_epoch`, and `max_extrapolation_us`.

The result composes a path and returns the transform that maps points expressed in `from_frame` into `to_frame`. Dynamic samples may be interpolated. Extrapolation is rejected beyond both the query limit and the edge limit. The result includes provenance for every traversed edge.

## Authority and conflicts

A single authority owns a directed edge within one session epoch unless an explicit selection policy exists. Concurrent authorities publishing the same edge are reported as a conflict rather than silently blended. Static calibration changes create a new calibration revision. Resettable localization creates a new session epoch and does not rewrite earlier history.

## Initial frame set

- `femto_bolt_color_optical_frame`
- `femto_bolt_depth_optical_frame`
- `femto_bolt_imu_frame`
- `body_base`
- `local_vio/<session-epoch>`

The current rigid head/body prototype publishes an identity body-to-IMU mounting edge. A future neck/kinematics Provider replaces that assumption with timestamped body-to-head transforms.
