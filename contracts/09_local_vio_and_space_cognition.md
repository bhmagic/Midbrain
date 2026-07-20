# Local VIO and Initialize Space Cognition Contract

Status: v0.3.8 working draft.

## Local VIO Resource Provider

The Provider continuously consumes ordered IMU observations, visual observations, camera/depth calibration, IMU calibration, and motion-inhibit state. It publishes:

- `localization.vio.status`
- `localization.body.pose`
- `localization.vio.bias`
- `transform.local_vio.body`

The current prototype treats head and body as one rigid pose. A future articulated neck Provider publishes timestamped body/head transforms through the Fabric graph.

Tracking states include `INITIALIZING`, `TRACKING`, `DEGRADED`, `LOST`, and `RESET_REQUIRED`. Every reset creates a new world frame `local_vio/<session-epoch>`. Position is initialized at `(0,0,0)`; roll and pitch are gravity aligned; yaw zero is the initial body-forward direction. The canonical local world uses `+Y` as up and `-Y` as gravity down.

## Inertial-first estimator requirement

The canonical estimator architecture is inertial propagation with visual correction:

1. Every ordered accelerometer and gyroscope sample propagates orientation, velocity, position, IMU biases, and uncertainty.
2. Camera observations update the propagated state and reduce accumulated drift.
3. Visual failure must not stop short-term inertial orientation propagation.
4. High-rate pose predictions may be published between camera updates without committing a second filter history.
5. Visual updates must be innovation-gated and may not overwrite the inertial state unconditionally.

The minimum reference state is:

- Orientation.
- Position.
- Velocity.
- Gyroscope bias.
- Accelerometer bias.
- State covariance or equivalent uncertainty representation.

The current reference backend is a 15-state error-state filter with pose-level RGB-D/IR corrections. A production backend may use a feature-level MSCKF, fixed-lag smoother, or nonlinear factor graph while preserving the same Provider outputs.

## Ordered IMU requirement

VIO must consume every retained IMU sample in order and detect gaps. Agent-level latest-value polling is not an acceptable high-rate transport. Camera and IMU timestamps must share a known domain or carry an explicit conversion and uncertainty. A Provider must not choose a different fallback clock field independently for each stream. The reference implementation prefers the SDK system timestamp consistently across video, accelerometer, and gyroscope inputs, then falls back uniformly when that field is unavailable.

A Provider must expose:

- Latest committed filter timestamp.
- Latest inertial prediction timestamp.
- Propagation sample or integration-step count.
- Detected IMU gaps.
- Estimated accelerometer and gyroscope biases.
- Pose uncertainty.

## Visual and depth corrections

RGB plus RGB-aligned depth is the preferred metric correction source. Depth provides metric feature geometry and translation constraints.

Infrared plus native depth is optional. It may be used when:

- IR, depth, and the current correction epoch are timestamp-compatible.
- IR intrinsics and IR-to-body/camera extrinsics are known.
- Depth is geometrically registered to the IR imager or transformed appropriately.
- The IR correction is materially more trustworthy than the RGB correction.

IR must not silently become a second unsynchronized pose authority. Published status identifies the accepted visual sensor, correction magnitude, reprojection error, feature support, and correction age.

## Initialize Space Cognition Skill

Subskills:

1. Acquire motion inhibit and establish stationary conditions.
2. Select and activate the head camera Provider.
3. Validate depth and other head scanning capabilities.
4. Validate the head IMU and its effective device calibration.
5. Activate/reset the Local VIO Provider.
6. Wait for stable tracking and publish initialized body pose.

Camera, depth, IR, and IMU activation is deduplicated when they belong to one physical Provider. The Skill normally runs once at agent startup and supports explicit forced reset.

## Runtime gravity stabilization

After initialization, gravity remains an observable roll/pitch reference. Dynamic acceleration must not be interpreted as gravity, and yaw is unaffected by the gravity reference.

Gravity correction:

- Changes rotation only.
- Preserves horizontal yaw.
- Never changes translation.
- Requires stable accelerometer magnitude/direction and bias-corrected gyroscope motion below the effective quiet threshold.
- Uses a small bounded step during normal tracking and a stronger bounded step during degraded recovery.

The nominal gyroscope quiet threshold is `0.012 rad/s`. Startup estimates zero-rate bias and a robust residual-noise ceiling. The effective threshold is the greater of the nominal threshold and `1.5 ×` the measured noise ceiling, clamped to `0.008..0.03 rad/s`.

## Low-light visual feature preprocessing

The baseline raw image path must remain available. Optional image normalization may add candidate features but must not silently replace a valid baseline result unless it satisfies a declared quality advantage.

The reference backend uses circular-kernel local contrast normalization for dim or low-contrast RGB and IR frames. Raw and normalized candidates are solved independently; the normalized candidate is selected only when it materially improves visual support.

## Reset and visualization epochs

Every forced reinitialization creates a new coordinate epoch. Derived maps must not mix points from different epochs. A visualization accumulator should suspend during reset, clear when the new epoch is accepted, reset its frame cursor and shared-memory reader, then resume automatically.

## Initialization control robustness

A reset or initialization request is accepted when the Provider has created the new session epoch. Failure to publish immediate diagnostic status must not convert an accepted reset into a failed control request. The initialization Skill waits for the VIO Provider to observe motion inhibit before resetting and may recover acceptance by observing a changed epoch after a transient Manager/control error.


## Initialization sample-rate diagnostics

The optional VIO status fields `initialization_accelerometer_window_count`, `initialization_gyroscope_window_count`, `initialization_accelerometer_rate_hz`, and `initialization_gyroscope_rate_hz` expose the exact stationary windows used by inertial initialization. Implementations must not require a fixed sample count to fit inside a duration that is shorter than the device's configured sampling period permits.
