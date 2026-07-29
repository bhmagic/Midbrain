# Changelog

## 0.1.0 - 2026-07-29

- Added a finite read-only validation contract for the actual RGB, native
  depth, IR, registered-depth, and overlay content rather than treating file
  presence as success.
- Added checks for independent grids, frame cadence, timestamps, registered
  boundaries, calibration revisions, shared-memory routes, numeric edge
  correspondence, and VLM image review.
- Added bounded configured-backend fallback and required backend provenance.
- Kept validation boot- and calibration-specific; an old successful review
  cannot validate a new camera boot or calibration revision.
- The manifest-bound implementation lives in Test Agent and produces local
  diagnostic images without commanding hardware or publishing a
  motion-usable transform.
