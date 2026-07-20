# Inertial-First Visual-Inertial Tracking Research Note

## Design question

The target behavior is the architecture commonly used in low-latency XR tracking: inertial measurements provide the high-rate motion model, while cameras constrain drift and correct accumulated error. The camera is therefore not the primary motion clock.

## Findings

### Filter-based systems

OpenVINS is a mature reference for this architecture. Its documented state propagation uses accelerometer and gyroscope measurements to evolve orientation, position, velocity, biases, and covariance. Its MSCKF visual updates use sparse feature tracks to correct that propagated state. It also documents fast state propagation for high-frequency pose output between camera updates.

Advantages for this project:

- Directly matches the intended IMU-first architecture.
- Explicit uncertainty and bias estimation.
- High-rate prediction between visual corrections.
- Supports camera/IMU extrinsic and time-offset calibration concepts.
- Includes zero-velocity and initialization support.

Constraints:

- GPL-3.0 licensing.
- Primarily C++/ROS-oriented integration.
- A direct code integration would affect package licensing and require a native adapter.

### Optimization-based systems

Basalt is a mature visual-inertial odometry and mapping system with a fixed-lag optimization architecture. It includes camera/IMU calibration and is distributed under BSD-3-Clause, although third-party dependencies must still be reviewed.

Advantages for this project:

- Permissive core license.
- Established visual-inertial optimization and mapping pipeline.
- Suitable long-term native backend candidate.

Constraints:

- Native C++ integration and dependency build are substantial.
- The current Femto Bolt streams and calibration must be adapted to Basalt input conventions.
- Windows runtime packaging needs dedicated validation.

### Depth-aided VIO

RGB-D VIO literature shows that depth can be integrated into visual-inertial estimation to improve metric geometry and reduce scale uncertainty. For the Femto Bolt, RGB-aligned depth is valuable because it supplies metric 3D points for visual corrections without relying on monocular scale observability.

### Infrared use

The Femto Bolt exposes IR and depth streams in addition to RGB. IR can provide useful texture when visible-light RGB is weak, but it should be treated as an optional synchronized visual measurement source. It must have valid intrinsics, known IR-to-color/IMU extrinsics, and timestamp-compatible depth. It should not operate as an independent unsynchronized pose authority.

## Selected implementation

The immediate backend is a 15-state inertial error-state filter:

- Orientation.
- Position.
- Velocity.
- Gyroscope bias.
- Accelerometer bias.
- Covariance.

Every ordered IMU sample propagates this state. RGB-D and optional IR/native-depth observations generate metric pose measurements. Those measurements pass innovation and Mahalanobis gates before correcting the filter. A non-committing fast propagation path publishes high-rate predicted poses between camera frames.

This is a reference implementation of the proven propagation/update architecture. It is not a full feature-level MSCKF and does not claim the accuracy of OpenVINS or Basalt before hardware and recorded-trajectory evaluation.

## Why this implementation was chosen now

- Corrects the architectural error in the earlier visual-first prototype.
- Reuses the verified Provider, shared-memory, calibration, transform, reset, and GUI infrastructure.
- Runs with the existing Python/OpenCV Windows environment.
- Keeps RGB-D metric corrections and introduces IR fallback without changing the camera Provider.
- Preserves the already tuned gravity leveling behavior.
- Maintains a backend boundary for later native Basalt or OpenVINS-class evaluation.

## Recommended evaluation sequence

1. Verify startup and stationary behavior on the Bolt.
2. Record synchronized RGB, aligned depth, IR, native depth, accelerometer, and gyroscope streams.
3. Measure high-rate orientation during fast pivots.
4. Measure position and velocity drift during temporary visual occlusion.
5. Compare RGB-D corrections against IR/depth corrections in the dim room.
6. Evaluate camera/IMU timestamp offset.
7. Replay the same recordings through the Python reference backend and a native Basalt/OpenVINS-class backend.
8. Select the production backend from measured accuracy, latency, robustness, licensing, and Windows packaging results.

## References

- OpenVINS documentation: IMU Propagation Derivations and Propagator class.
- OpenVINS repository and MSCKF documentation.
- Basalt repository and VIO/mapping documentation.
- DVIO: Depth Aided Visual Inertial Odometry for RGBD Sensors.
- PIVO: Probabilistic Inertial-Visual Odometry for Occlusion-Robust Navigation.
- Orbbec Femto Bolt Hardware Specifications.
