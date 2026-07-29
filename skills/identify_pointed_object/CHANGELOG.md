# Changelog

## 0.1.0 - 2026-07-29

- Added a finite, discoverable, read-only skill contract for identifying which
  visible object a person is pointing at.
- Limited model input to a bounded scene question and required current RGB
  evidence, backend provenance, uncertainty, and local diagnostic capture.
- Explicitly excluded depth inference, 3D registration, planning, contact, and
  physical motion. Execution is supplied by the manifest-bound Test Agent
  adapter rather than code in this manifest-only package.
