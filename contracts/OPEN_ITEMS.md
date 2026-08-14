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
- Preserve the implemented arm-scene level-of-detail contract: explicit
  semantic depth uses a bounded hand-centric 4 pi projection with 4,096
  near-uniform Fibonacci directions by default, one nearest hit per occupied
  direction, a 5 mm close-range radius floor, and range-scaled conservative
  spheres. The legacy unclaimed point-cloud path retains its 0.5 m
  gripper/20 mm and 1.2 m arm-base/60 mm tiers when diagnostic publication is
  explicitly enabled.
- Physically qualify the five-second `WORK_OBJECT` `VISIBLE_SURFACE_AABB`
  planning extent and its arm-base-aligned named corners. It is current
  visible-surface evidence, not tracked solid geometry and not a controller
  collision primitive; obstacles remain sphere-only.
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
- Physically qualify the independent Contact Work Provider and the initial
  non-clamping slicing Skill. Complete the six-degree-of-freedom wrench boundary,
  blade development-v3 acting-point measurements, locked-joint disturbance
  tests, saturation behavior, command-replacement/watchdog races, and thermal
  policy before making slicing eligible in the default Agent tool set.
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
- Replace the temporary `fixed_rig_initial_hold` pose-publication policy only
  after live RGB-D/IMU camera tracking passes recorded and physical stationary
  drift, trajectory, outage, and reacquisition limits.
- Add an explicit fixed/mobile/articulated camera authority state. A reviewed
  fixed-camera transform, live VIO, and future body/head kinematics must never
  compete as unlabeled camera-pose authorities in the same transform query.
- Add persistent visual-map or external-anchor relocalization so mobile VIO can
  correct world-origin and yaw drift instead of relying only on incremental
  frame-to-frame odometry.
- Define feature-level visual update schemas if backend diagnostics are exposed beyond pose-level corrections.
- Define neck/body kinematic authority when articulated hardware exists.
- Add expiry/fencing and hard enforcement to motion-inhibit and physical Control Authority Leases.
- Hardware-validate policy-aware graceful-stop timeout behavior, safety-critical process preservation, and explicit complete process-tree force termination.
- Define transform covariance tangent convention and conflict-selection policy beyond rejection.
- Implement the versioned gripper-motion arm-root alignment contract described
  in `docs/13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md`: Fabric-hosted synchronized
  correspondences, non-collinear rigid fitting, fit visualization, Manager
  activation/rollback, and optional translation-only close-range refinement.
