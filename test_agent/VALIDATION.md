# Validation

## v0.2.9

- JavaScript syntax validation covers initialization window count/rate diagnostics.


Validated in the delivery environment:

- Five initialization and point-cloud lifecycle regression tests.
- Recovery when reset changed epoch despite a transient control HTTP error.
- Waiting for VIO motion-inhibit acknowledgement before reset.
- Forced-reinitialization map lifecycle and pause during degraded VIO.
- Transient recycled BufferRef classification.
- Browser JavaScript syntax with Node.
- Orthographic isometric projection, world-down arrow, camera frustum, inertial propagation, visual correction, and gravity diagnostics.
- Python source compilation and wheel construction.

Still required on the target workspace:

- Automatic startup initialization reaches `SUCCEEDED` with Local VIO v0.2.2.
- High-rate inertial pose updates remain smooth between camera frames.
- Visual correction source and staleness match physical camera conditions.
- IR/depth fallback appears only when synchronized and materially stronger than RGB-D.
- Map remains spatially consistent during moderate and fast camera motion.


## v0.2.8

- Initialization display surfaces IMU history counts, timestamp skew, and the exact backend blocker.
- An earlier failed startup attempt is shown as superseded while a later initialization is active or successful.
