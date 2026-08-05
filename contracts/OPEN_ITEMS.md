# Open Contract Items

- Formalize clock-domain conversion, drift, offset uncertainty, and online camera/IMU time-offset estimation.
- Replace consumer-independent `freshness_ms` enforcement with passive temporal
  evidence and Skill-owned, versioned temporal policies.
- Require timestamped Fabric observations for semantic scenes, controller
  previews, authorization decisions, execution outcomes, operation progress,
  and lease/fencing state without putting Fabric in the motor-control loop.
- Physically qualify the implemented HOT `world_model.arm_scene_compiler`
  owner. It now supplies monotonic revision, per-source TTL, current robot
  self-filter geometry, canonical ROI layers, explicit keep-out authority,
  and Fabric-regulated point-cloud/assertion inputs. Remaining contract work is
  persistent transform/session lineage, confidence/uncertainty propagation for
  every source, multi-publisher semantic stream arbitration, and exact scene
  lineage binding through preview, authorization, and execution.
- Preserve the implemented arm-scene ROI and level-of-detail contract: 0.5 m around the
  measured gripper with a 20 mm minimum sphere radius, 1.2 m around the arm base
  with a 60 mm minimum sphere radius, conservative inflation of smaller
  geometry, and voxel/cluster merging before canonical publication.
- Preserve the implemented semantic-policy authority and precedence: only
  user/upstream-described `KEEP_OUT` objects become blocking geometry,
  unclaimed visible geometry is ignored `PUSHABLE` telemetry, the selected
  manipulation target becomes `WORKPIECE`, and explicit `NO_CONTACT` overrides
  workpiece-default acting-frame contact.
- Complete live-camera qualification of the upgraded discoverable generic item
  locator. Its typed result now includes stable object identity, 2D evidence,
  metric point/volume or bearing-only fallback, frame, timestamp, uncertainty,
  material/depth-validity status, and the next useful active-perception action.
- Physically qualify and extend the implemented non-error-only depth fallback ladder: robust
  registered-depth samples on the object, reprojected native depth, bounded
  same-surface neighboring depth, task-plane ray intersection (for example the
  current-effector-altitude plane), support-plane/known-size constraints,
  multi-view or arm-motion parallax, and finally bearing-only/image-servo
  evidence. A reflective or transparent target must never silently inherit
  background depth, and degraded evidence must not be promoted to collision
  authority.
- Extend the initially physically qualified reusable no-contact composition
  loop. The 2026-08-04 live toilet-paper run completed 191 mm and 27.8 mm
  measured-arrival corrections and stopped after re-observation proved the
  requested standoff. Remaining qualification covers diverse scenes,
  deliberate scene expiry/source loss, increasing-residual rejection, and
  reflective/transparent targets.
- Define a controller-owned multistep route contract with an immutable goal,
  complete route preview, bounded local-replan envelope, per-leg evidence, and
  no need for an Agent command at every intermediate waypoint.
- Define versioned arm operating profiles that distinguish manufacturer and
  protocol maxima, official arm configuration, Basic provider caps, and
  physically qualified autonomous limits.
- Extend the implemented provider-local leased arm-retention contract with
  cross-provider authority lineage and longer physical qualification. `FLOAT`,
  bounded 2x-4x Kp compliant MIT hold, and POS_VEL position lock now capture a
  measured endpoint and expose owner, lease, expiry, keepalive, and
  release-to-float behavior.
- Add BufferRef acquire/release leases and producer-death invalidation.
- Move the transitional expired-slot recovery out of consuming Skills and into
  Fabric/provider-owned coherent-bundle materialization. Skills may dereference
  Fabric-issued BufferRefs, but must not query Provider control APIs or select
  unadvertised data-plane slots. Fabric should return or pin the best available
  synchronized snapshot with immutable timestamp and lineage.
- Add event subscriptions and deterministic recording/replay.
- Hardware-validate the 15-state inertial-first reference backend on the Femto Bolt.
- Add a recorded-data evaluation harness with trajectory, innovation, and covariance-consistency metrics.
- Compare the reference backend against a native Basalt adapter and an OpenVINS/MSCKF evaluation build.
- Define feature-level visual update schemas if backend diagnostics are exposed beyond pose-level corrections.
- Define neck/body kinematic authority when articulated hardware exists.
- Add expiry/fencing and hard enforcement to motion-inhibit and physical Control Authority Leases.
- Hardware-validate policy-aware graceful-stop timeout behavior, safety-critical process preservation, and explicit complete process-tree force termination.
- Define transform covariance tangent convention and conflict-selection policy beyond rejection.
- Implement the versioned gripper-motion arm-root alignment contract described
  in `docs/13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md`: Fabric-hosted synchronized
  correspondences, non-collinear rigid fitting, fit visualization, Manager
  activation/rollback, and optional translation-only close-range refinement.
