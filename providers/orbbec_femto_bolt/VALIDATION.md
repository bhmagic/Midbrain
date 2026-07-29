# Validation

Python parsing, synthetic serial-bound calibration creation/load/apply, six-position affine recovery, custom calibration backup/write, and static web-asset checks passed.

Seven software tests now also cover flexible independent RGB/IR/depth grids,
provider-written alignment metadata, the generic shared-memory descriptor, the
direct Orbbec fallback, and atomic publication of both routes in one route set.

The standalone GUI still requires validation on Windows against a running Femto Bolt CameraHost shared-memory mapping. Verify all six captures, live Provider reload, and post-calibration gravity magnitude on the target physical device.
