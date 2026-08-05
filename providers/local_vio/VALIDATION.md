# Validation

Run the Local VIO suite from its component environment:

```powershell
.\providers\local_vio\.venv\Scripts\python.exe -m pytest -q providers\local_vio\python\tests
```

The stopped suite covers ordered IMU propagation, sample-rate-independent
stationary initialization, bias and gravity gates, RGB-D correction, IR/depth
fallback selection, innovation rejection, reset and epoch behavior,
observation sequencing, convention metadata, BufferRef failures, and recovery
from incomplete initialization evidence.

Live validation must confirm the installed camera/IMU calibration, plausible
sample rates, propagation between visual corrections, quiet gravity recovery,
visual-outage behavior, current convention-versioned frames, and clean epoch
transition after reset.

Deployment qualification still requires deterministic synchronized replay,
external trajectory ground truth, absolute and relative trajectory error,
stationary drift, visual-outage drift, reacquisition discontinuity,
camera/IMU time-offset estimation, and target-machine timing measurements.
