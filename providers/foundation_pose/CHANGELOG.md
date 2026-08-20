# Changelog

## 1.1.2 - 2026-08-19

- Declare the score-network output as an uncalibrated raw ranking value and
  state explicitly that the Provider defines no absolute acceptance threshold.
- Retain the compatibility `quality.score` field while adding the unambiguous
  `ranking_score_raw` and `score_semantics` fields.
- Prefer an installed Python 3.11 application and safely tolerate a broken
  Windows `py.exe` launcher during setup discovery.

## 1.1.1 - 2026-08-18

- Replace the placeholder blue JSON page with a theme-matched, read-only
  runtime surface showing residency, native readiness, exact latest CAD and
  RGB/depth/mask evidence, score, timing, and connection diagnostics.
- Serve the development page as separate HTML, CSS, and JavaScript assets while
  retaining generic known-object-pose Provider ownership only.

## 1.1.0 - 2026-08-18

- Replace the Linux-derived multi-purpose integration with the Windows-native
  single-function CUDA/TensorRT known-object-pose Provider.
