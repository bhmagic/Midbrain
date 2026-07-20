# Validation

## Automated source checks completed during cleanup

The cleaned repository was validated with the source directories on `PYTHONPATH`:

- Orbbec calibration tests: 2 passed.
- Local VIO tests: 30 passed.
- Test Agent initialization and point-cloud tests: 5 passed.
- Total: 37 passed.

The tests cover six-position accelerometer solving, calibration persistence, sample-rate-independent initialization, inertial propagation, RGB-D correction behavior, IR fallback selection, gravity gating, reset/epoch behavior, observation sequence, initialization recovery, and point-cloud lifecycle.

## PowerShell validation command

```powershell
.\scripts\validate.ps1
```

The script checks JSON and Python syntax, runs all Python tests, builds Python wheels into a temporary validation directory, checks Rust formatting/tests/release build when Cargo is available, and configures/builds CameraHost only when explicitly requested with valid Orbbec SDK paths.

## Hardware acceptance

### Startup

- Camera Provider becomes `HOT`.
- Auto-initialization reaches `SUCCEEDED`.
- Initialization blocker is `none`.
- Selected accelerometer and gyro windows meet the required count.
- Inferred sample rates are plausible.
- IMU propagation steps increase.
- Body pose is non-null.

### Rotation and correction

- Fast pivots continue updating orientation through inertial propagation.
- Visual correction can become stale without freezing pose.
- Reacquisition reduces drift without a large discontinuity.
- Gravity is disabled during obvious angular motion and becomes ready/active only after quiet stabilization.

### Mapping

- Map Capture becomes `CAPTURING` after pose and RGB-D are present.
- Point count increases.
- Forced reinitialization creates a new epoch and resumes capture.
- Old-epoch points are not mixed with the new world frame.

### Calibration

- All six orientations pass quality checks.
- The calibration file is backed up and replaced atomically.
- Provider reload applies the new revision.
- Corrected stationary acceleration is physically plausible.

## Platform-specific compile status

The Python source and tests are platform-independent. The Rust core can be built on Linux or Windows, but the operational release target is Windows MSVC. CameraHost is Windows- and Orbbec-SDK-dependent. A Linux cleanup environment cannot provide a hardware-valid CameraHost build.

## Remaining qualification

The current validation is not formal localization certification. Still required:

- deterministic synchronized recording/replay
- external trajectory ground truth
- absolute and relative trajectory error
- stationary velocity and position drift
- fast-pivot latency and orientation error
- visual-outage drift and reacquisition discontinuity
- camera/IMU time-offset estimation
- CPU and scheduling measurements on the target Windows system

## Automatic Rust formatting

`validate.ps1` runs `cargo fmt --all` before the strict `cargo fmt --all -- --check` step. The first command applies the formatter from the installed Rust toolchain to every workspace crate, including platform-gated Windows code. The second command verifies that the resulting tree is fully formatted. Successful validation then refreshes all SHA-256 file manifests.
